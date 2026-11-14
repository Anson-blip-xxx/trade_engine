"""Versioned cash valuation, committed only inside an immutable settlement.

Venue cash and mathematical fill economics are different measurements. This
module never creates a balancing adjustment. A candidate is not authorization:
the caller must still prove complete income, ownership and exact wallet equality.
"""

from decimal import Decimal

from v2_core.evidence import canonical, digest
from v2_core.precision import amount, decimal_text, money_context

VERSION = "venue-cash-basis-v1"


def fill_facts(rows):
    """Rows use the ledger query's six legacy columns plus immutable evidence."""
    facts = []
    for leg, qty, price, fee, currency, key, evidence, occurred, order_id in rows:
        if (
            leg not in {"OPEN", "CLOSE"}
            or currency != "USDT"
            or evidence.get("source") != "binance-userTrades"
            or not evidence.get("trade_id")
        ):
            raise ValueError("VENUE_CASH_FILL_EVIDENCE_REQUIRED")
        value = amount(evidence["venue_realized_pnl"])
        if leg == "OPEN" and value:
            raise ValueError("OPEN_FILL_REALIZED_PNL_NONZERO")
        facts.append(
            {
                "key": key,
                "order_id": order_id,
                "leg": leg,
                "quantity": decimal_text(qty),
                "price": decimal_text(price),
                "fee": decimal_text(fee),
                "fee_currency": currency,
                "occurred_at_ms": occurred,
                "evidence": evidence,
                "venue_realized_pnl": decimal_text(value),
            }
        )
    if not facts or len({f["key"] for f in facts}) != len(facts):
        raise ValueError("UNIQUE_VENUE_CASH_FILLS_REQUIRED")
    return sorted(facts, key=lambda f: f["key"])


def _economics(report, facts, cash_rows):
    if (
        report["settlement_currency"] != "USDT"
        or report["missing_valuations"]
        or Decimal(report["opened_quantity"]) <= 0
        or Decimal(report["opened_quantity"]) != Decimal(report["closed_quantity"])
    ):
        raise ValueError("CLOSED_USDT_CASH_BASIS_REQUIRED")
    for row in cash_rows:
        # New venue basis cannot coexist with any theoretical bridge. Only
        # previously verified real funding/insurance cash is in this path.
        _, currency, _, kind, evidence = row
        if currency != "USDT" or not (
            (
                kind == "FUNDING"
                and evidence.get("income_id")
                and isinstance(evidence.get("allocation"), dict)
            )
            or (
                kind == "CORRECTION"
                and evidence.get("source") == "verified-liquidation-insurance-clear-v1"
                and evidence.get("income_id")
            )
        ):
            raise ValueError("VENUE_CASH_REJECTS_UNCLASSIFIED_ADJUSTMENT")
    with money_context():
        actual = sum((Decimal(f["venue_realized_pnl"]) for f in facts), Decimal(0))
        fees = sum((Decimal(f["fee"]) for f in facts), Decimal(0))
        cash = sum((r[0] for r in cash_rows), Decimal(0))
        if fees != Decimal(report["fees"]) or cash != Decimal(
            report["cash_adjustments"]
        ):
            raise ValueError("VENUE_CASH_LEDGER_COMPONENT_MISMATCH")
        net = actual - fees + cash
    return actual, fees, cash, net


def build_basis(report, *, scope, facts, cash_rows, analysis, income_digest):
    actual, fees, cash, net = _economics(report, facts, cash_rows)
    if (
        scope["exchange"] != "BINANCE"
        or scope["environment"] != "SANDBOX"
        or scope["product"] != "FUTURES"
        or analysis.get("intent_id") != report["intent_id"]
        or Decimal(analysis["actual_gross_pnl"]) != actual
        or Decimal(analysis["theoretical_gross_pnl"]) != Decimal(report["gross_pnl"])
        or not isinstance(income_digest, str)
        or len(income_digest) != 64
        or any(c not in "0123456789abcdef" for c in income_digest)
    ):
        raise ValueError("VENUE_CASH_BASIS_INPUT_MISMATCH")
    return {
        "version": VERSION,
        "episode_id": report["intent_id"],
        "account_scope": scope,
        "currency": "USDT",
        "ledger_revision": report["accounting_revision"],
        "fills_digest": digest(canonical({"fills": facts})),
        "cash_facts_digest": digest(
            canonical(
                {
                    "cash": sorted(
                        [
                            {
                                "amount": decimal_text(a),
                                "currency": c,
                                "key": k,
                                "kind": kind,
                                "evidence": ev,
                            }
                            for a, c, k, kind, ev in cash_rows
                        ],
                        key=lambda row: row["key"],
                    )
                }
            )
        ),
        "income_digest": income_digest,
        "actual_gross_pnl": decimal_text(actual),
        "fees": decimal_text(fees),
        "real_cash_adjustments": decimal_text(cash),
        "net_pnl": decimal_text(net),
        "theoretical_gross_pnl": report["gross_pnl"],
        "analysis": analysis,
    }


def apply_basis(report, basis, *, scope, facts, cash_rows, expected_income_digest):
    if not isinstance(basis, dict) or basis.get("version") != VERSION:
        raise ValueError("UNSUPPORTED_SETTLEMENT_CASH_BASIS")
    if basis.get("income_digest") != expected_income_digest:
        raise ValueError("SETTLEMENT_CASH_BASIS_INCOME_CHANGED")
    expected = build_basis(
        report,
        scope=scope,
        facts=facts,
        cash_rows=cash_rows,
        analysis=basis["analysis"],
        income_digest=basis["income_digest"],
    )
    if basis != expected:
        raise ValueError("SETTLEMENT_CASH_BASIS_FACTS_CHANGED")
    return {
        **report,
        "gross_pnl": basis["actual_gross_pnl"],
        "net_pnl": basis["net_pnl"],
        "theoretical_gross_pnl": report["gross_pnl"],
        "pnl_basis": VERSION,
        "cash_analysis": basis["analysis"],
        "explanation": "已核验交易所现金盈亏、费用及实际资金调整；理论价差单独保留",
    }
