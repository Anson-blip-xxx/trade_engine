"""Fresh per-symbol risk prices verified against PG and incremental CH history."""

from services.v2_risk_reference import SOURCE, ReferenceUnavailable, bound
from services.v2_s3_candles import build_frame
from services.v2_s3_source import frame_digest
from v2_core.account_risk import RiskProvenance, RiskReference
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol


class SharedRiskReference:
    def __init__(self, connect, *, scope, archive, clock_ms, max_age_ms=90000):
        if (
            scope.environment != "SANDBOX"
            or not callable(clock_ms)
            or type(max_age_ms) is not int
            or not 1 <= max_age_ms <= 90000
        ):
            raise ValueError("SHARED_REFERENCE_SCOPE_REQUIRED")
        self.connect, self.scope, self.archive, self.clock, self.max_age = (
            connect,
            scope,
            archive,
            clock_ms,
            max_age_ms,
        )

    def _head(self, target):
        with self.connect() as c:
            return c.execute(
                "SELECT h.observed_at_ms,h.frame_id,f.input_digest FROM v2_market_symbol_heads h "
                "JOIN v2_s3_frames f ON f.environment=h.environment AND f.frame_id=h.frame_id "
                "WHERE h.environment=%s AND h.symbol=%s AND h.status='CURRENT'",
                (self.scope.environment, target),
            ).fetchone()

    def __call__(self, request):
        if not bound(request, self.scope) or request.get("leg") != "OPEN":
            raise ReferenceUnavailable("REFERENCE_REQUEST_SCOPE")
        target = symbol(request.get("symbol"))
        head = self._head(target)
        if head is None:
            raise ReferenceUnavailable("REFERENCE_NOT_CONFIRMED")
        observed, frame_id, fingerprint = head
        deadline = observed + self.max_age
        if not observed <= self.clock() < deadline:
            raise ReferenceUnavailable("REFERENCE_STALE")
        bars = self.archive.read(target, observed - 86400000, observed)
        batch = {
            "source": "BINANCE_FUTURES",
            "environment": self.scope.environment,
            "interval": "1m",
            "closed_at": observed,
            "candles": {target: bars},
        }
        frame = build_frame(batch, environment=self.scope.environment)
        if frame_digest(frame) != fingerprint:
            raise ReferenceUnavailable("REFERENCE_INPUT_MISMATCH")
        if self._head(target) != head:
            raise ReferenceUnavailable("REFERENCE_SUPERSEDED")
        if not observed <= self.clock() < deadline:
            raise ReferenceUnavailable("REFERENCE_STALE")
        return RiskReference(
            self.scope.environment,
            target,
            SOURCE,
            bars[0]["c"],
            observed,
            valid_until_ms=deadline,
            provenance=RiskProvenance(frame_id, digest(canonical(batch)), fingerprint),
        )
