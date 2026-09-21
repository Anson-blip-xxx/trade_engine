"""Testnet close-all protection protocol journal; not a PM or risk reservation.

One-way, exclusive symbol ownership is a caller precondition. A triggered algo
is NOT a filled close: actualOrderId still needs the order/fill reconciliation.
No replacement, bulk cancellation, live transport or automatic write retry.
"""

import json
import re
from dataclasses import asdict, dataclass

from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.state import StateKey, normalized
from v2_core.transport import ExchangeTransportError


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
            if payload["status"] == "REJECTED":
                return {"status": "REJECTED", "error": payload["submission_error"]}
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
                submission_error={
                    "class": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "http_status": getattr(exc, "status", None),
                },
            )
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
        if payload["status"] == "REJECTED":
            return {"status": "REJECTED", "error": payload["submission_error"]}
        return self.query(spec)

    def query(self, spec):
        snapshot, payload = self.read(spec)
        if snapshot is None:
            raise ValueError("unregistered protection")
        raw = self.request(
            "GET",
            "/fapi/v1/algoOrder",
            {"clientAlgoId": self.params(spec)["clientAlgoId"]},
        )
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
        payload.update(status=raw["algoStatus"], observation=raw)
        if not self.save(spec, snapshot, payload):
            return {"status": "CONCURRENT_CHANGE"}
        return {
            "status": raw["algoStatus"],
            "algo_id": raw["algoId"],
            "client_algo_id": raw["clientAlgoId"],
        }

    def cancel_flat_once(self, spec):
        """Flat-account test cleanup only; not safe replacement of a live stop."""
        result = self.query(spec)
        if result["status"] != "NEW":
            return result
        snapshot, payload = self.read(spec)
        if payload.get("cancel_started"):
            return {**result, "status": "CANCEL_PENDING_QUERY_ONLY"}
        rows = self.request("GET", "/fapi/v3/positionRisk", {})
        if not isinstance(rows, list) or any(
            amount(p["positionAmt"]) != 0 for p in rows
        ):
            raise ValueError("cancel requires flat account")
        payload.update(cancel_started=True)
        if not self.save(spec, snapshot, payload):
            return {"status": "CONCURRENT_CHANGE"}
        # On lost response the cancel_started latch persists. Later calls query
        # only; they cannot remove a different order or issue another DELETE.
        self.request("DELETE", "/fapi/v1/algoOrder", {"algoId": result["algo_id"]})
        return self.query(spec)
