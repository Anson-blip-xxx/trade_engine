"""Exact pre-submit price/stop-risk drift check, not a fill-price guarantee."""

from fractions import Fraction

from v2_core.errors import SubmissionNotSent
from v2_core.ledger import amount


def validate_entry_price(*, side, original, mark, stop, frozen, current):
    if not (
        frozen["entry.price_guard_enabled"] or current["entry.price_guard_enabled"]
    ):
        return None
    if side not in {"BUY", "SELL"}:
        raise SubmissionNotSent("DIRECTIONAL_SUBMISSION_PRICE_INVALID")
    try:
        original, mark, stop = (
            Fraction(amount(x, positive=True)) for x in (original, mark, stop)
        )
    except (ValueError, TypeError):
        raise SubmissionNotSent("DIRECTIONAL_SUBMISSION_PRICE_INVALID") from None
    sign = 1 if side == "BUY" else -1
    original_risk, actual_risk = sign * (original - stop), sign * (mark - stop)
    active = [p for p in (frozen, current) if p["entry.price_guard_enabled"]]
    max_drift = min(Fraction(p["entry.max_adverse_price_fraction"]) for p in active)
    max_risk = min(Fraction(p["entry.max_stop_risk_increase"]) for p in active)
    drift = sign * (mark - original) / original
    if original_risk <= 0 or actual_risk <= 0:
        raise SubmissionNotSent("DIRECTIONAL_SUBMISSION_STOP_INVALID")
    if drift > max_drift or actual_risk > original_risk * max_risk:
        raise SubmissionNotSent("DIRECTIONAL_SUBMISSION_PRICE_MOVED")
    return {
        "source": "directional-pre-submit-price-v1",
        "side": side,
        "reference_price": str(original),
        "mark_price": str(mark),
        "adverse_fraction": str(drift),
        "stop_risk_ratio": str(actual_risk / original_risk),
        "max_adverse_fraction": str(max_drift),
        "max_stop_risk_increase": str(max_risk),
        "execution_authorized": False,
    }
