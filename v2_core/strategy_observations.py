"""Sampled mark excursions, explicitly not tick-complete MFE/MAE."""

from decimal import Decimal

from v2_core.ledger import amount


def observe_excursions(previous, *, now_ms, return_pct):
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("EXCURSION_CLOCK_INVALID")
    current = amount(return_pct)
    if previous:
        if now_ms < previous["last_observed_ms"]:
            raise ValueError("EXCURSION_CLOCK_REGRESSION")
        if now_ms == previous["last_observed_ms"]:
            # Repeated evaluation of the same timestamp is not another sample.
            return previous
    return {
        "version": "mark-samples-v1",
        "coverage": "SAMPLED_MARKS_NOT_FULL_PRICE_PATH",
        "first_observed_ms": previous["first_observed_ms"] if previous else now_ms,
        "last_observed_ms": now_ms,
        "samples": previous["samples"] + 1 if previous else 1,
        "mfe_return_pct": str(
            max(
                Decimal(previous["mfe_return_pct"]) if previous else Decimal(0), current
            )
        ),
        "mae_return_pct": str(
            min(
                Decimal(previous["mae_return_pct"]) if previous else Decimal(0), current
            )
        ),
    }
