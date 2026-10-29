"""Testnet close-all protection protocol journal; not a PM or risk reservation.

One-way, exclusive symbol ownership is a caller precondition. A triggered algo
is NOT a filled close: actualOrderId still needs the order/fill reconciliation.
No replacement, bulk cancellation, live transport or automatic write retry.
"""

import json
import re
import time
from dataclasses import asdict, dataclass

from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.state import StateKey, normalized
from v2_core.transport import ExchangeTransportError


def protection_error_evidence(exc):
    """Keep actionable, bounded transport facts without persisting messages."""
    result = {"class": type(exc).__name__}
    if isinstance(exc, ExchangeTransportError):
        # Use the transport implementation rather than an arbitrary exception's
        # method/attributes: response text, request URLs and credentials are not
        # diagnostic evidence that is safe to persist or send to an operator.
        safe = ExchangeTransportError.diagnostic_evidence(exc)
        result.update(
            category=safe["category"],
            code=safe.get("exchange_code"),
            http_status=safe.get("http_status"),
        )
    return result


@dataclass(frozen=True)
class ProtectionSpec:
    episode: str
    symbol: str
    side: str
    kind: str
    trigger_price: str

    def __post_init__(self):
        normalized(self.episode)
        if (
            not re.fullmatch(r"[A-Z0-9]{1,30}USDT", self.symbol)
            or self.side not in {"BUY", "SELL"}
            or self.kind not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
        ):
            raise ValueError("unsupported protection")
        amount(self.trigger_price, positive=True)


class TestnetProtection:
    __test__ = False

    def __init__(self, store, request, *, scope):
        if (
            scope.environment != "SANDBOX"
            or scope.exchange != "BINANCE"
            or scope.product != "FUTURES"
            or request.account_id != scope.account_id
            or request.environment != scope.environment
        ):
            raise ValueError("testnet protection scope mismatch")
        self.store, self.request, self.scope = store, request, scope

    def key(self, spec):
        # Price is not identity: changing the same leg must fail, not silently
        # create a second stop. Explicit replacement belongs to the future PM.
        return StateKey(
            **asdict(self.scope),
            namespace="testnet-protection-v1",
            key=canonical(
                {"episode": spec.episode, "symbol": spec.symbol, "kind": spec.kind}
            ),
        )

    def params(self, spec):
        return {
            "algoType": "CONDITIONAL",
            "symbol": spec.symbol,
            "side": spec.side,
            "positionSide": "BOTH",
            "type": spec.kind,
            "triggerPrice": spec.trigger_price,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "false",
            "clientAlgoId": "v2p-" + digest(self.key(spec).identity)[:28],
        }

    def read(self, spec):
        snapshot = self.store.read(self.key(spec))
        if snapshot is None:
            return None, None
        payload = json.loads(snapshot.payload_json)
        if snapshot.deleted or payload["spec"] != asdict(spec):
            raise ValueError("protection identity conflict")
        return snapshot, payload

    def save(self, spec, snapshot, payload):
        version = 0 if snapshot is None else snapshot.version
        return (
            self.store.change(
                self.key(spec),
                expected_version=version,
                request_key=f"transition:{version + 1}",
                payload=payload,
                reason="TESTNET_PROTECTION_PROTOCOL",
            ).code
            == "APPLIED"
        )

    def submit_once(self, spec):
        snapshot, payload = self.read(spec)
        if snapshot is not None:
            if payload["status"] in {"REJECTED", "NOT_SENT"}:
                return {
                    "status": payload["status"],
                    "error": payload.get("submission_error", {}),
                }
            return self.query(spec)
        # Commit before network. Crashes at any later instruction recover by
        # the original clientAlgoId; not-found never authorizes a second POST.
        payload = {"spec": asdict(spec), "status": "SENDING"}
        if not self.save(spec, None, payload):
            return {"status": "CONCURRENT_CHANGE"}
        snapshot, payload = self.read(spec)
        try:
            raw = self.request("POST", "/fapi/v1/algoOrder", self.params(spec))
        except Exception as exc:  # noqa: BLE001 - ambiguous writes are never retried
            payload.update(
                status="UNKNOWN",
                submission_error=protection_error_evidence(exc),
            )
            # These two gates fail before constructing/sending an HTTP request.
            # Distinguish them from an ambiguous network outcome. This is NOT
            # permission to retry a POST: the guarded lifecycle can instead
            # reduce the unprotected exposure, preserving the original identity.
            if isinstance(exc, ExchangeTransportError) and payload[
                "submission_error"
            ].get("category") in {"QUOTA_DENIED", "ENDPOINT_OR_WRITE_DISABLED"}:
                payload["status"] = "NOT_SENT"
            # Explicit parameter rejection only. Do not treat a timeout,
            # duplicate ID, unknown error or not-found query as non-execution.
            if (
                isinstance(exc, ExchangeTransportError)
                and exc.status == 400
                and exc.code == -4007
            ):
                payload["status"] = "REJECTED"
        else:
            payload.update(status="ACK_RECEIVED", submission=raw)
        if not self.save(spec, snapshot, payload):
            return {"status": "CONCURRENT_CHANGE"}
        if payload["status"] in {"REJECTED", "NOT_SENT"}:
            return {
                "status": payload["status"],
                "error": payload.get("submission_error", {}),
            }
        return self.query(spec)

    def query(self, spec):
        snapshot, payload = self.read(spec)
        if snapshot is None:
            raise ValueError("unregistered protection")
        if payload["status"] == "EXTERNAL_FLAT_RECONCILED":
            # This is a local reconciliation marker, not a venue order status.
            # The supervisor validates its independent evidence and ledger; no
            # caller may interpret it as a venue CANCELED/EXPIRED confirmation.
            return {"status": "EXTERNAL_FLAT_RECONCILED"}
        if payload["status"] in {"REJECTED", "NOT_SENT"}:
            return {
                "status": payload["status"],
                "error": payload.get("submission_error", {}),
            }
        # Visibility retries are useful immediately after an ACK, not for a
        # permanently absent UNKNOWN parent scanned every trading cycle. An
        # absent UNKNOWN order remains ambiguous and never authorizes a POST.
        attempts = 5 if payload["status"] == "ACK_RECEIVED" else 1
        for attempt in range(attempts):
            try:
                raw = self.request(
                    "GET",
                    "/fapi/v1/algoOrder",
                    {"clientAlgoId": self.params(spec)["clientAlgoId"]},
                )
                break
            except ExchangeTransportError as exc:
                if exc.code != -2013 or attempt == attempts - 1:
                    raise
                time.sleep(0.25)  # newly ACKed algos may not be query-visible yet
        expected = self.params(spec)
        for field in (
            "symbol",
            "side",
            "positionSide",
            "algoType",
            "clientAlgoId",
            "workingType",
        ):
            if raw.get(field) != expected[field]:
                raise ValueError("protection query identity mismatch")
        if (
            raw.get("orderType") != spec.kind
            or raw.get("closePosition") is not True
            or raw.get("priceProtect") is not False
            or amount(raw["triggerPrice"], positive=True) != amount(spec.trigger_price)
            or type(raw.get("algoId")) is not int
            or raw["algoId"] <= 0
            or (
                payload.get("observation") is not None
                and raw["algoId"] != payload["observation"]["algoId"]
            )
            or raw.get("algoStatus")
            not in {
                "NEW",
                "CANCELED",
                "TRIGGERING",
                "TRIGGERED",
                "FINISHED",
                "REJECTED",
                "EXPIRED",
            }
        ):
            raise ValueError("protection query terms mismatch")
        updated = {**payload, "status": raw["algoStatus"], "observation": raw}
        if updated != payload and not self.save(spec, snapshot, updated):
            return {"status": "CONCURRENT_CHANGE"}
        return {
            "status": raw["algoStatus"],
            "algo_id": raw["algoId"],
            "client_algo_id": raw["clientAlgoId"],
        }

    def cancel_flat_once(self, spec, *, symbol_only=False):
        """Flat-account test cleanup only; not safe replacement of a live stop."""
        result = self.query(spec)
        if result["status"] != "NEW":
            return result
        snapshot, payload = self.read(spec)
        if payload.get("cancel_started"):
            return {**result, "status": "CANCEL_PENDING_QUERY_ONLY"}
        rows = self.request("GET", "/fapi/v3/positionRisk", {})
        if type(symbol_only) is not bool:
            raise ValueError("explicit cleanup scope required")
        if symbol_only:
            if not isinstance(rows, list) or any(
                not isinstance(p, dict)
                or not isinstance(p.get("symbol"), str)
                or p.get("positionSide") != "BOTH"
                for p in rows
            ):
                raise ValueError("invalid symbol cleanup inventory")
            rows = [p for p in rows if p["symbol"] == spec.symbol]
        if not isinstance(rows, list) or any(
            amount(p["positionAmt"]) != 0 for p in rows
        ):
            raise ValueError("cancel requires flat account")
        payload.update(cancel_started=True)
        if not self.save(spec, snapshot, payload):
            return {"status": "CONCURRENT_CHANGE"}
        # On lost response the cancel_started latch persists. Later calls query
        # only; they cannot remove a different order or issue another DELETE.
        try:
            self.request("DELETE", "/fapi/v1/algoOrder", {"algoId": result["algo_id"]})
        except ExchangeTransportError as exc:
            if exc.code != -2011:
                raise
            # Closing can auto-expire a close-all order between GET and DELETE.
            # A rejection alone is not proof: query the same identity again.
            observed = self.query(spec)
            if observed["status"] in {"CANCELED", "EXPIRED"}:
                return observed
            raise
        return self.query(spec)
