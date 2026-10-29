"""Prove account cash ownership from immutable venue fills, never symbol alone."""

from dataclasses import asdict
from decimal import Decimal

from v2_core.ledger import amount
from v2_core.precision import money_context


def income_owner(conn, scope, row):
    if row.get("income_type") == "INSURANCE_CLEAR":
        # Only independently verified exchange-created forced closes qualify.
        from contextlib import nullcontext

        from v2_core.liquidation_cash import insurance_clear_candidate
        from v2_core.service import TradingData

        episodes = conn.execute(
            """SELECT DISTINCT i.intent_id::text FROM v2_trade_intents i
            JOIN v2_orders o ON o.episode_id=i.intent_id JOIN v2_fills f USING(order_id)
            WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
            AND i.payload->>'symbol'=%s AND o.leg='CLOSE' AND o.status='FILLED'
            AND o.request_evidence->>'origin'='BINANCE_EXTERNAL_CLOSE'
            AND o.request_evidence->>'kind'='LIQUIDATION'
            AND f.occurred_at_ms BETWEEN %s AND %s LIMIT 2""",
            (*asdict(scope).values(), row["symbol"], row["occurred_at_ms"] - 999,
             row["occurred_at_ms"] + 999),
        ).fetchall()
        if len(episodes) != 1:
            raise ValueError("AMBIGUOUS_INSURANCE_CLEAR_OWNERSHIP")
        episode = episodes[0][0]
        trace = TradingData(lambda: nullcontext(conn)).trace(episode, connection=conn)
        insurance_clear_candidate(trace, row)
        return episode
    evidence = row.get("evidence", {})
    if (
        row.get("currency") != "USDT"
        or evidence.get("source") != "binance-income"
        or row.get("income_type") not in {"COMMISSION", "REALIZED_PNL", "FUNDING_FEE"}
    ):
        raise ValueError("UNATTRIBUTED_ACCOUNT_CASH")
    rows = conn.execute(
        """SELECT i.intent_id::text,o.leg,f.quantity::text,f.fee::text,
        f.fee_currency,f.occurred_at_ms,f.payload
        FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
        JOIN v2_fills f USING(order_id)
        WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
        AND i.payload->>'symbol'=%s ORDER BY f.occurred_at_ms,f.fill_key""",
        (*asdict(scope).values(), row["symbol"]),
    ).fetchall()
    owners = set()
    with money_context():
        if row["income_type"] == "FUNDING_FEE":
            if evidence.get("trade_id") != "":
                raise ValueError("AMBIGUOUS_FUNDING_OWNERSHIP")
            balances = {}
            for episode, leg, quantity, _, _, when, proof in rows:
                if when > row["occurred_at_ms"]:
                    continue
                if when == row["occurred_at_ms"]:
                    raise ValueError("AMBIGUOUS_FUNDING_OWNERSHIP")
                if proof.get("source") != "binance-userTrades":
                    raise ValueError("UNVERIFIED_CASH_OWNER")
                balances[episode] = balances.get(episode, Decimal(0)) + (
                    amount(quantity, positive=True) * (1 if leg == "OPEN" else -1)
                )
                if balances[episode] < 0:
                    raise ValueError("INVALID_EXPOSURE_TIMELINE")
            owners = {episode for episode, balance in balances.items() if balance > 0}
        else:
            for episode, _, _, fee, currency, when, proof in rows:
                if proof.get("trade_id") != evidence.get("trade_id"):
                    continue
                if (
                    proof.get("source") != "binance-userTrades"
                    or currency != "USDT"
                    or abs(when - row["occurred_at_ms"]) > 999
                ):
                    raise ValueError("UNVERIFIED_CASH_OWNER")
                expected = (
                    -amount(fee)
                    if row["income_type"] == "COMMISSION"
                    else amount(proof["venue_realized_pnl"])
                )
                if expected != amount(row["amount"]):
                    raise ValueError("INCOME_FILL_MISMATCH")
                owners.add(episode)
    if len(owners) != 1:
        raise ValueError("AMBIGUOUS_CASH_OWNERSHIP")
    return owners.pop()
