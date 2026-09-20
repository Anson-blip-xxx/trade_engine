"""Archive-backed admission prices. No credentials, network construction or startup.

Closed-candle prices are historical references, NOT executable quotes/mark prices.
The source publisher and archive client are trusted deployment dependencies; hashes
prove consistency with PG receipts, not authenticity against a malicious publisher.
"""

import json

from services.v2_s3_candles import build_frame
from services.v2_s3_source import frame_digest
from v2_core.account_risk import AccountScope, RiskProvenance, RiskReference
from v2_core.errors import SubmissionNotSent
from v2_core.evidence import digest
from v2_core.ingress import milliseconds, symbol
from v2_core.runner import RiskVerdict
from v2_core.runtime import DataRuntime

SOURCE = "binance-closed-1m-v1"


class ReferenceUnavailable(ValueError):
    """Fixed non-sensitive failure code; no order state or external side effects."""


def bound(request, scope):
    return isinstance(request, dict) and all(
        request.get(key) == getattr(scope, key)
        for key in ("exchange", "account_id", "environment", "product")
    )


class ArchivedRiskReference:
    def __init__(self, connect, *, scope, archive, clock_ms, max_age_ms):
        if (
            not isinstance(scope, AccountScope)
            or not callable(clock_ms)
            or not callable(getattr(archive, "get", None))
        ):
            raise TypeError("explicit scope, archive and clock required")
        if type(max_age_ms) is not int or not 1 <= max_age_ms <= 3600000:
            raise ValueError("bounded reference age required")
        self._connect, self.scope, self.archive = connect, scope, archive
        self.clock_ms, self.max_age_ms = clock_ms, max_age_ms

    def _head(self):
        with self._connect() as conn:
            return conn.execute(
                """SELECT d.frame_id,d.observed_at_ms,d.expires_at_ms,d.max_age_ms,
                d.archive_digest,d.input_digest,d.status,f.input_digest,
                EXISTS(SELECT 1 FROM v2_candle_delivery_events e WHERE e.environment=d.environment
                    AND e.frame_id=d.frame_id AND e.outcome='ACKNOWLEDGED')
                FROM v2_candle_deliveries d LEFT JOIN v2_s3_frames f
                ON f.environment=d.environment AND f.frame_id=d.frame_id
                WHERE d.environment=%s ORDER BY d.observed_at_ms DESC,d.frame_id DESC LIMIT 1""",
                (self.scope.environment,),
            ).fetchone()

    def __call__(self, request):
        if not bound(request, self.scope) or request.get("leg") != "OPEN":
            raise ReferenceUnavailable("REFERENCE_REQUEST_SCOPE")
        target = symbol(request.get("symbol"))
        head = self._head()
        if head is None:
            raise ReferenceUnavailable("REFERENCE_MISSING")
        (
            frame_id,
            observed,
            expires,
            source_age,
            archive_hash,
            input_hash,
            status,
            published_hash,
            acknowledged,
        ) = head
        if status != "ACKNOWLEDGED" or published_hash != input_hash or not acknowledged:
            raise ReferenceUnavailable("REFERENCE_NOT_CONFIRMED")
        deadline = min(expires, observed + source_age + 1, observed + self.max_age_ms)
        started = milliseconds(self.clock_ms())
        if not observed <= started < deadline:
            raise ReferenceUnavailable("REFERENCE_STALE")
        encoded = self.archive.get(archive_hash)
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 8_000_000
            or digest(encoded) != archive_hash
        ):
            raise ReferenceUnavailable("REFERENCE_ARCHIVE_UNAVAILABLE")
        try:
            batch = json.loads(encoded)
            frame = build_frame(batch, environment=self.scope.environment)
            if (
                frame["frame_id"] != frame_id
                or frame["observed_at"] != observed
                or frame_digest(frame) != input_hash
            ):
                raise ValueError("receipt mismatch")
            # Use the exact original close, not rounded floating-point indicators.
            price = batch["candles"][target][0]["c"]
        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise ReferenceUnavailable("REFERENCE_INPUT_MISMATCH") from None
        if self._head() != head:
            raise ReferenceUnavailable("REFERENCE_SUPERSEDED")
        finished = milliseconds(self.clock_ms())
        if not started <= finished < deadline:
            raise ReferenceUnavailable("REFERENCE_STALE")
        return RiskReference(
            self.scope.environment,
            target,
            SOURCE,
            str(price),
            observed,
            valid_until_ms=deadline,
            provenance=RiskProvenance(frame_id, archive_hash, input_hash),
        )


def create_guarded_runtime(
    connect,
    *,
    scope,
    archive,
    clock_ms,
    max_reference_age_ms,
    submit,
    query,
    risk_check,
    enabled=False,
    projectors=(),
):
    """Scope-bound composition. Network/write clients and risk policy stay explicit.

    `enabled` only gates the supplied submit port; it is not production approval.
    No policy is installed here. Missing policy fails closed during submission CAS.
    """
    if type(enabled) is not bool or not all(
        callable(port) for port in (submit, query, risk_check)
    ):
        raise ValueError("explicit ports and boolean write gate required")
    provider = ArchivedRiskReference(
        connect,
        scope=scope,
        archive=archive,
        clock_ms=clock_ms,
        max_age_ms=max_reference_age_ms,
    )

    def risk(request):
        if not bound(request, scope):
            return RiskVerdict(False, "runtime account scope mismatch")
        return risk_check(request)

    def send(request):
        if not bound(request, scope) or not enabled:
            raise SubmissionNotSent("ENDPOINT_OR_WRITE_DISABLED")
        return submit(request)

    def recover(request):
        if not bound(request, scope):
            raise ReferenceUnavailable("RECOVERY_REQUEST_SCOPE")
        return query(request)

    return DataRuntime(
        connect,
        submit=send,
        query=recover,
        risk_check=risk,
        clock_ms=clock_ms,
        projectors=projectors,
        risk_reference=provider,
        scope=scope,
    )
