"""Versioned, exact per-fill proof for observed Binance USDT cash truncation.

This is not an epsilon allowance or a claim about every Binance product. Only
single-opening, fully closed linear episodes with no later increases qualify.
The caller must ALSO prove income/fill and account-wallet equality. Unknown
rounding conventions, reordered exposure and unrepresentable corrections fail.
"""

from decimal import Decimal
from fractions import Fraction

from v2_core.precision import amount, decimal_text, money_context

MODEL = "binance-usdt-per-fill-truncate-8-v1"


def rounding_proof(trace, report):
    orders = {o["order_id"]: o for o in trace["orders"]}
    opening = [f for f in trace["fills"] if orders[f["order_id"]]["leg"] == "OPEN"]
    closing = [f for f in trace["fills"] if orders[f["order_id"]]["leg"] == "CLOSE"]
    if (
        not opening
        or not closing
        or len({f["order_id"] for f in opening}) != 1
        or len(opening) + len(closing) != len(trace["fills"])
        or max(f["occurred_at_ms"] for f in opening)
        >= min(f["occurred_at_ms"] for f in closing)
        or trace["request"]["side"] not in {"BUY", "SELL"}
    ):
        raise ValueError("UNSUPPORTED_PNL_PRECISION_TIMELINE")

    def quantity(fill):
        return Fraction(amount(fill["quantity"], positive=True))

    def price(fill):
        return Fraction(amount(fill["price"], positive=True))

    qty = sum(quantity(f) for f in opening)
    if qty != sum(quantity(f) for f in closing):
        raise ValueError("EXPOSURE_NOT_CLOSED")
    if any(amount(f["evidence"]["venue_realized_pnl"]) != 0 for f in opening):
        raise ValueError("OPEN_FILL_REALIZED_PNL_NONZERO")
    entry = sum(quantity(f) * price(f) for f in opening) / qty
    sign = 1 if trace["request"]["side"] == "BUY" else -1
    theoretical, venue, proofs = Fraction(0), Fraction(0), []
    for fill in sorted(
        closing, key=lambda f: (f["occurred_at_ms"], f["evidence"]["trade_id"])
    ):
        exact = quantity(fill) * (price(fill) - entry) * sign
        observed = amount(fill["evidence"]["venue_realized_pnl"])
        # int(Fraction) truncates toward zero, including losses. No Decimal
        # division may move a value across a rounding boundary here.
        truncated = Fraction(int(exact * 100_000_000), 100_000_000)
        if Fraction(observed) not in {exact, truncated}:
            raise ValueError("UNEXPLAINED_FILL_PNL_PRECISION")
        theoretical += exact
        venue += Fraction(observed)
        proofs.append(
            {
                "trade_id": fill["evidence"]["trade_id"],
                "theoretical_numerator": str(exact.numerator),
                "theoretical_denominator": str(exact.denominator),
                "venue_pnl": decimal_text(observed),
                "method": "EXACT"
                if Fraction(observed) == exact
                else "TRUNCATE_TOWARD_ZERO_8",
            }
        )
    if theoretical != Fraction(Decimal(report["gross_pnl"])):
        raise ValueError("VENUE_LEDGER_ECONOMICS_MISMATCH")
    delta = venue - theoretical
    with money_context():
        correction = Decimal(delta.numerator) / Decimal(delta.denominator)
    encoded = decimal_text(correction)
    if Fraction(amount(encoded)) != delta:
        raise ValueError("PNL_CORRECTION_NOT_EXACTLY_REPRESENTABLE")
    return {
        "model": MODEL,
        "amount": encoded,
        "theoretical_gross_pnl": report["gross_pnl"],
        "fills": proofs,
    }
