"""Exact, replayable price envelopes for advisory sizing and locked admission.

An envelope budgets price drift, not a promise about market-order execution.
The final admission guard must still re-read aggregate reservations and price.
"""

from decimal import Decimal

from v2_core.ledger import amount
from v2_core.precision import money_context


def price_envelope(
    *, price, mark_price, drift_fraction, side, stop_price, stop_fraction, cost_fraction
):
    px, mark, stop_px = (
        amount(x, positive=True) for x in (price, mark_price, stop_price)
    )
    drift = amount(drift_fraction)
    stop = amount(stop_fraction, positive=True)
    cost = amount(cost_fraction)
    if side not in {"LONG", "SHORT"} or not 0 <= drift <= Decimal(".05") or cost < 0:
        raise ValueError("INVALID_SIZING_PRICE_ENVELOPE")
    with money_context():
        high = max(px, mark) * (1 + drift)
        low = min(px, mark) * (1 - drift)
        # Existing fixed stops remain authoritative; do not move the stop to
        # make a stale signal look affordable. Either edge can violate a stop.
        valid = low > stop_px if side == "LONG" else high < stop_px
        loss_distance = high - stop_px if side == "LONG" else stop_px - low
        risk = max(high * (stop + cost), loss_distance + high * cost)
        return {
            "reference_price": str(px),
            "mark_price": str(mark),
            "price_drift_fraction": str(drift),
            "price_high": str(high),
            "price_low": str(low),
            "unit_stop_risk": str(risk),
            "stop_valid_through_envelope": valid,
        }


def fixed_stop_risk(
    *, quantity, notional, stop_price, side, stop_fraction, cost_fraction
):
    """Do not understate risk when a fresh reference moves away from a fixed stop."""
    qty = amount(quantity, positive=True)
    stop_px = amount(stop_price, positive=True)
    stop, cost = amount(stop_fraction, positive=True), amount(cost_fraction)
    if side not in {"LONG", "SHORT"} or cost < 0:
        raise ValueError("CAPITAL_STOP_PLAN_UNVERIFIED")
    with money_context():
        # Multiplication/cross comparison avoid non-terminating entry-price
        # division and keep NUMERIC quantity boundaries deterministic.
        stop_notional = qty * stop_px
        if (side == "LONG" and stop_notional >= notional) or (
            side == "SHORT" and stop_notional <= notional
        ):
            raise ValueError("CAPITAL_STOP_ON_WRONG_SIDE")
        distance = abs(notional - stop_notional)
        return max(notional * (stop + cost), distance + notional * cost)
