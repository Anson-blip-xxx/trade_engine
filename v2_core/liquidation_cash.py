"""Narrow insurance-clear ownership for verified, fully filled forced closes.

An empty tradeId is never assigned from symbol/time alone. The immutable close
order must carry the exchange's autoclose identity and complete order snapshot.
Final settlement independently proves exact account-wallet equality.
"""

from decimal import Decimal

from v2_core.evidence import canonical, digest
from v2_core.ledger import amount
from v2_core.precision import money_context


def insurance_clear_candidate(trace, row):
    if (
        row.get("income_type") != "INSURANCE_CLEAR"
        or row.get("currency") != "USDT"
        or row.get("symbol") != trace["request"]["symbol"]
        or row.get("evidence") != {"source": "binance-income", "trade_id": ""}
        or amount(row["amount"]) >= 0
        or type(row.get("occurred_at_ms")) is not int
    ):
        raise ValueError("UNVERIFIED_INSURANCE_CLEAR")
    candidates = []
    for order in trace["orders"]:
        binding = order.get("request_evidence", {})
        if (
            order["leg"] != "CLOSE"
            or order["status"] != "FILLED"
            or binding.get("origin") != "BINANCE_EXTERNAL_CLOSE"
            or binding.get("kind") != "LIQUIDATION"
        ):
            continue
        raw = binding.get("venue_order", {})
        venue_id = binding.get("exchange_order_id")
        client_id = binding.get("venue_client_order_id")
        if (
            not isinstance(venue_id, str)
            or not venue_id.isascii()
            or not venue_id.isdecimal()
            or int(venue_id) <= 0
            or str(raw.get("orderId")) != venue_id
            or not isinstance(client_id, str)
            or not client_id.startswith("autoclose-")
            or raw.get("clientOrderId") != client_id
            or raw.get("symbol") != row["symbol"]
            or raw.get("side")
            != ("SELL" if trace["request"]["side"] == "BUY" else "BUY")
            or raw.get("positionSide") != "BOTH"
            or raw.get("status") != "FILLED"
            or raw.get("reduceOnly") is not True
            or raw.get("type") not in {"LIMIT", "MARKET"}
        ):
            raise ValueError("UNVERIFIED_LIQUIDATION_ORDER")
        fills = [f for f in trace["fills"] if f["order_id"] == order["order_id"]]
        if not fills or any(
            f["evidence"].get("source") != "binance-userTrades"
            or not f["evidence"].get("trade_id")
            or f["fee_currency"] != "USDT"
            for f in fills
        ):
            raise ValueError("UNVERIFIED_LIQUIDATION_FILLS")
        with money_context():
            filled = sum(
                (amount(f["quantity"], positive=True) for f in fills), Decimal(0)
            )
        if not (
            filled
            == amount(order["quantity"], positive=True)
            == amount(raw["executedQty"], positive=True)
            == amount(raw["origQty"], positive=True)
        ):
            raise ValueError("LIQUIDATION_QUANTITY_MISMATCH")
        if any(abs(f["occurred_at_ms"] - row["occurred_at_ms"]) <= 999 for f in fills):
            candidates.append((order, binding))
    if len(candidates) != 1:
        raise ValueError("AMBIGUOUS_INSURANCE_CLEAR_OWNERSHIP")
    order, binding = candidates[0]
    return {
        "income_id": row["income_id"],
        "source_id": row["source_id"],
        "amount": str(amount(row["amount"])),
        "occurred_at_ms": row["occurred_at_ms"],
        "order_id": order["order_id"],
        "exchange_order_id": binding["exchange_order_id"],
        "binding_digest": digest(canonical(binding)),
    }
