"""Validate persisted, confirmed Testnet maintenance evidence before adoption."""

import json
from contextlib import nullcontext
from uuid import UUID

from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey

ORIGIN = "APPROVED_TESTNET_MAINTENANCE"


def confirmed_close(conn, episode, case_id):
    case_id = str(UUID(str(case_id)))
    row = conn.execute(
        """SELECT i.exchange,i.account_id,i.environment,i.product,i.payload,o.leg
        FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
        WHERE i.intent_id=%s AND o.order_id=%s""",
        (episode, case_id),
    ).fetchone()
    if row is None or (row[0], row[2], row[3], row[5]) != (
        "BINANCE",
        "SANDBOX",
        "FUTURES",
        "CLOSE",
    ):
        raise ValueError("MAINTENANCE_CASE_SCOPE_MISMATCH")
    store = BusinessState(lambda: nullcontext(conn))

    def read(suffix):
        state = store.read(
            StateKey(
                *row[:4],
                namespace="testnet-scoped-cleanup-v1",
                key=case_id + ":" + suffix,
            )
        )
        if state is None or state.deleted:
            raise ValueError("CONFIRMED_MAINTENANCE_REQUIRED")
        return json.loads(state.payload_json)

    plan, close = read("plan"), read("close")
    actions = [a for a in plan["actions"] if a["key"] == "close"]
    if (
        len(actions) != 1
        or close.get("status") != "CONFIRMED"
        or close["action"] != actions[0]
    ):
        raise ValueError("CONFIRMED_MAINTENANCE_REQUIRED")
    p, raw = actions[0]["params"], close["proof"]["order"]
    side = "SELL" if row[4]["side"] == "BUY" else "BUY"
    if (
        plan["symbol"] != row[4]["symbol"]
        or any(
            p.get(k) != v
            for k, v in {
                "symbol": row[4]["symbol"],
                "side": side,
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": "true",
            }.items()
        )
        or any(
            raw.get(k) != v
            for k, v in {
                "symbol": p["symbol"],
                "side": side,
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": True,
                "status": "FILLED",
                "clientOrderId": p["newClientOrderId"],
            }.items()
        )
    ):
        raise ValueError("MAINTENANCE_ORDER_IDENTITY_MISMATCH")
    if (
        type(raw.get("orderId")) is not int
        or raw["orderId"] <= 0
        or any(
            amount(raw[k], positive=True) != amount(p["quantity"], positive=True)
            for k in ("origQty", "executedQty")
        )
    ):
        raise ValueError("MAINTENANCE_QUANTITY_MISMATCH")
    binding = {
        "origin": ORIGIN,
        "case_id": case_id,
        "exchange_order_id": str(raw["orderId"]),
        "venue_client_order_id": raw["clientOrderId"],
    }
    return row[:4], p, raw, binding


def require_binding(conn, episode, binding, quantity, request_key):
    _, params, _, expected = confirmed_close(conn, episode, binding.get("case_id"))
    if (
        binding != expected
        or request_key != "maintenance:" + expected["case_id"]
        or amount(params["quantity"]) != quantity
    ):
        raise ValueError("MAINTENANCE_BINDING_MISMATCH")
