"""One explicitly approved legacy-account cleanup, NOT a strategy executor.

Fixed testnet account/campaign and two approved symbols. Every external write is
preceded by a PG CAS. Ambiguous actions are query-only on restart, never resent.
Maintenance evidence is separate from new-strategy trade performance.
"""

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from decimal import Decimal
from uuid import uuid4

from services.v2_testnet_inventory import credentials, deployment_database
from v2_core.account_inventory import AccountInventory, persist_inventory, positions
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.public_market import PublicRateBudget
from v2_core.state import BusinessState, StateKey
from v2_core.transport import BinanceSignedTransport

SCOPE = AccountScope("BINANCE", "v2-testnet-primary", "SANDBOX", "FUTURES")
CAMPAIGN = "approved-legacy-cleanup-20260921"
APPROVED = {"UBUSDT": "SELL", "SAGAUSDT": "BUY"}
OLD_UNITS = tuple(
    "trade-" + name + ".service" for name in ("s0", "s3", "s6", "s8", "sentiment", "tv")
)


def state_key(key):
    return StateKey(
        **asdict(SCOPE), namespace="testnet-maintenance-v1", key=CAMPAIGN + ":" + key
    )


def make_plan(observation):
    if observation["account_scope"] != asdict(SCOPE) or set(observation["blockers"]) - {
        "EXISTING_POSITION_REQUIRES_RECOVERY",
        "EXISTING_CONDITIONAL_ORDERS",
    }:
        raise ValueError("INVALID_CLEANUP_BASELINE")
    raw = observation["responses"]
    if raw["ordinary_orders"] or len(raw["conditional_orders"]) > 100:
        raise ValueError("UNAPPROVED_ORDER_SET")
    actions = []
    for row in raw["positions_after"]:
        qty = amount(row["positionAmt"])
        if not qty:
            continue
        target = row["symbol"]
        side = "SELL" if qty > 0 else "BUY"
        if row["positionSide"] != "BOTH" or APPROVED.get(target) != side:
            raise ValueError("UNAPPROVED_POSITION")
        identity = (
            "v2clean-"
            + digest(canonical({"campaign": CAMPAIGN, "symbol": target}))[:24]
        )
        actions.append(
            {
                "key": "close:" + target,
                "kind": "CLOSE",
                "fingerprint": {
                    key: row[key]
                    for key in (
                        "symbol",
                        "positionSide",
                        "positionAmt",
                        "entryPrice",
                        "updateTime",
                    )
                },
                "params": {
                    "symbol": target,
                    "side": side,
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "quantity": str(abs(qty)),
                    "reduceOnly": "true",
                    "newClientOrderId": identity,
                    "newOrderRespType": "RESULT",
                },
            }
        )
    for row in raw["conditional_orders"]:
        if type(row["algoId"]) is not int or row["algoId"] < 0:
            raise ValueError("INVALID_ALGO_ID")
        actions.append(
            {
                "key": "cancel:" + str(row["algoId"]),
                "kind": "CANCEL",
                "symbol": row["symbol"],
                "params": {"algoId": row["algoId"]},
            }
        )
    if len({a["key"] for a in actions}) != len(actions):
        raise ValueError("DUPLICATE_ACTION")
    return {
        "observation_id": observation["observation_id"],
        "campaign": CAMPAIGN,
        "actions": actions,
    }


class Cleanup:
    def key(self, action):
        return state_key(action["key"])

    def __init__(self, store, request, *, pause=time.sleep):
        if (request.account_id, request.environment) != (SCOPE.account_id, "SANDBOX"):
            raise ValueError("TESTNET_ONLY")
        self.store, self.request = store, request
        self.pause = pause

    def flat(self, *, check_orders=True):
        if positions(self.request("GET", "/fapi/v3/positionRisk", {})):
            raise ValueError("ACCOUNT_NOT_FLAT")
        if check_orders and self.request("GET", "/fapi/v1/openOrders", {}) != []:
            raise ValueError("ORDINARY_ORDERS_PRESENT")

    def preflight(self, action):
        if action["kind"] == "CLOSE" and (
            self.request("GET", "/fapi/v1/positionSide/dual", {}).get(
                "dualSidePosition"
            )
            is not False
        ):
            raise ValueError("ONE_WAY_REQUIRED")
        if action["kind"] == "CLOSE":
            p = action["params"]
            rows = self.request("GET", "/fapi/v3/positionRisk", {"symbol": p["symbol"]})
            active = [
                r
                for r in rows
                if r["symbol"] == p["symbol"] and amount(r["positionAmt"])
            ]
            if not active:
                return False
            if len(active) != 1 or any(
                active[0][key] != value for key, value in action["fingerprint"].items()
            ):
                raise ValueError("OLD_POSITION_CHANGED")
            if p["reduceOnly"] != "true" or APPROVED.get(p["symbol"]) != p["side"]:
                raise ValueError("REDUCTION_ONLY")
            return True
        # Recheck exposure before each exact-ID cancellation. Full ordinary-order
        # inventory is checked at the batch boundaries, not 100 times (weight 40).
        self.flat(check_orders=False)
        raw = self.request("GET", "/fapi/v1/algoOrder", action["params"])
        if (
            raw["symbol"] != action["symbol"]
            or raw["algoId"] != action["params"]["algoId"]
        ):
            raise ValueError("ALGO_IDENTITY_MISMATCH")
        if raw["algoStatus"] in {"CANCELED", "EXPIRED", "REJECTED", "FINISHED"}:
            return False
        if raw["algoStatus"] != "NEW":
            raise ValueError("ALGO_REQUIRES_RECONCILIATION")
        return True

    def verify(self, action):
        if action["kind"] == "CANCEL":
            raw = self.request("GET", "/fapi/v1/algoOrder", action["params"])
            if (raw.get("algoId"), raw.get("symbol"), raw.get("algoStatus")) != (
                action["params"]["algoId"],
                action["symbol"],
                "CANCELED",
            ):
                raise ValueError("CANCEL_UNCONFIRMED")
            return {"order": raw}
        p = action["params"]
        raw = self.request(
            "GET",
            "/fapi/v1/order",
            {"symbol": p["symbol"], "origClientOrderId": p["newClientOrderId"]},
        )
        expected = {
            "symbol": p["symbol"],
            "clientOrderId": p["newClientOrderId"],
            "side": p["side"],
            "positionSide": "BOTH",
            "reduceOnly": True,
            "type": "MARKET",
            "status": "FILLED",
        }
        if any(raw.get(k) != v for k, v in expected.items()) or any(
            amount(raw[k]) != amount(p["quantity"]) for k in ("origQty", "executedQty")
        ):
            raise ValueError("CLOSE_UNCONFIRMED")
        fills = self.request(
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": p["symbol"], "orderId": raw["orderId"], "limit": 1000},
        )
        if not isinstance(fills, list) or not 0 < len(fills) < 1000:
            raise ValueError("INCOMPLETE_CLEANUP_FILLS")
        seen, quantity = set(), Decimal(0)
        for fill in fills:
            if (
                fill["symbol"],
                fill["orderId"],
                fill["side"],
                fill["positionSide"],
            ) != (p["symbol"], raw["orderId"], p["side"], "BOTH") or fill["id"] in seen:
                raise ValueError("FILL_IDENTITY_MISMATCH")
            seen.add(fill["id"])
            quantity += amount(fill["qty"], positive=True)
            amount(fill["commission"])
            amount(fill["realizedPnl"])
        if quantity != amount(p["quantity"]):
            raise ValueError("FILL_QUANTITY_MISMATCH")
        return {"order": raw, "fills": fills}

    def run_action(self, action):
        key = self.key(action)
        current = self.store.read(key)
        if current is None:
            needed = self.preflight(action)
            value = {
                "action": action,
                "status": "SENDING" if needed else "ALREADY_ABSENT",
            }
            claim = self.store.change(
                key,
                expected_version=0,
                request_key="claim",
                payload=value,
                reason="APPROVED_TESTNET_CLEANUP",
            )
            if claim.code == "APPLIED" and needed:
                try:
                    response = self.request(
                        "POST" if action["kind"] == "CLOSE" else "DELETE",
                        "/fapi/v1/order"
                        if action["kind"] == "CLOSE"
                        else "/fapi/v1/algoOrder",
                        action["params"],
                    )
                    value = {**value, "status": "ACK_RECEIVED", "response": response}
                except Exception:  # noqa: BLE001 - all unknown outcomes are query-only, no exception text
                    value = {**value, "status": "UNKNOWN"}
                self.store.change(
                    key,
                    expected_version=1,
                    request_key="response",
                    payload=value,
                    reason="CLEANUP_RESPONSE",
                )
            current = self.store.read(key)
        value = json.loads(current.payload_json)
        if value["action"] != action:
            raise ValueError("ACTION_CONFLICT")
        if value["status"] in {"CONFIRMED", "ALREADY_ABSENT"}:
            return value["status"]
        # A successful cancellation may be visible in query a little later.
        # Retry only reads, with a small finite bound; never resend the DELETE.
        for attempt in range(5):
            try:
                proof = self.verify(action)
                break
            except ValueError as exc:
                if (
                    action["kind"] != "CANCEL"
                    or str(exc) != "CANCEL_UNCONFIRMED"
                    or attempt == 4
                ):
                    raise
                self.pause(0.5)
        saved = self.store.change(
            key,
            expected_version=current.version,
            request_key="confirmed",
            payload={**value, "status": "CONFIRMED", "proof": proof},
            reason="CLEANUP_VERIFIED",
        )
        if saved.code not in {"APPLIED", "ALREADY_APPLIED"}:
            raise ValueError("CLEANUP_CONFIRMATION_CONFLICT")
        return "CONFIRMED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--execute-approved-cleanup", action="store_true", required=True
    )
    args = parser.parse_args()
    for unit in OLD_UNITS:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        if result.stdout.strip() != "inactive":
            raise ValueError("OLD_TRADING_SERVICE_ACTIVE")
    connect = deployment_database()
    store = BusinessState(connect)
    cfg = credentials(args.config, notify=False)
    clock = lambda: time.time_ns() // 1000000
    budget = PublicRateBudget(connect, scope="v2-testnet-host-maintenance", limit=1800)

    def permit(method, path):
        weights = {
            "/fapi/v1/openOrders": 40,
            "/fapi/v1/openAlgoOrders": 40,
            "/fapi/v1/positionSide/dual": 30,
            "/fapi/v1/multiAssetsMargin": 30,
            "/fapi/v3/account": 5,
            "/fapi/v3/positionRisk": 5,
            "/fapi/v1/userTrades": 5,
        }
        remaining = weights.get(path, 1)
        while remaining:
            chunk = min(remaining, 10)
            if not budget.permit(chunk):
                return False
            remaining -= chunk
        return True

    request = BinanceSignedTransport(
        account_id=SCOPE.account_id,
        environment="SANDBOX",
        api_key=cfg["BINANCE_TESTNET_API_KEY"],
        api_secret=cfg["BINANCE_TESTNET_API_SECRET"],
        clock_ms=clock,
        permit=permit,
        enable_trading=True,
        enable_testnet_cancellation=True,
    )
    current = store.read(state_key("plan"))
    if current is None:
        observation = AccountInventory(request, scope=SCOPE, clock_ms=clock).collect(
            str(uuid4())
        )
        persist_inventory(connect, scope=SCOPE, observation=observation)
        plan = make_plan(observation)
        saved = store.change(
            state_key("plan"),
            expected_version=0,
            request_key="plan",
            payload=plan,
            reason="USER_APPROVED_OLD_TESTNET_BASELINE",
        )
        if saved.code not in {"APPLIED", "ALREADY_APPLIED"}:
            raise ValueError("PLAN_CONFLICT")
    else:
        plan = json.loads(current.payload_json)
    worker = Cleanup(store, request)
    cancellation_phase = False
    for action in plan["actions"]:
        if action["kind"] == "CANCEL" and not cancellation_phase:
            worker.flat()
            cancellation_phase = True
        status = worker.run_action(action)
        print(json.dumps({"action": action["key"], "status": status}), flush=True)
    worker.flat()
    after = AccountInventory(request, scope=SCOPE, clock_ms=clock).collect(str(uuid4()))
    print(
        json.dumps(persist_inventory(connect, scope=SCOPE, observation=after)),
        flush=True,
    )
    if after["blockers"]:
        raise ValueError("ACCOUNT_NOT_CLEAR")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - no credential-bearing error text
        print(
            json.dumps(
                {"status": "CLEANUP_STOPPED", "error_class": type(exc).__name__}
            ),
            flush=True,
        )
        raise SystemExit(1) from None
