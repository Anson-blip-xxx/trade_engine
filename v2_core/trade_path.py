"""Factual gross PnL excursions from fills and closed bars, never a trade permit.

Fill-changing and boundary minutes are NOT used for intrabar extrema. Prices at
fills and complete constant-inventory minutes give lower bounds on excursions,
not executable peak profits. All inventory arithmetic is exact rational math.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from itertools import pairwise

from v2_core.quant_indicators import MINUTE, Bar, decimal

VERSION = "fill-aware-trade-path-v1"


def number(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.rounding = ROUND_HALF_EVEN
        result = Decimal(value.numerator) / Decimal(value.denominator)
        return format(result.quantize(Decimal("1e-18")), "f")


@dataclass(frozen=True)
class PathFill:
    identity: str
    t: int
    leg: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self):
        if (
            not isinstance(self.identity, str)
            or not 1 <= len(self.identity) <= 512
            or type(self.t) is not int
            or not 0 <= self.t < 2**53
            or self.leg not in {"OPEN", "CLOSE"}
        ):
            raise ValueError("PATH_FILL_IDENTITY")
        for field in ("quantity", "price"):
            value = decimal(getattr(self, field))
            if value <= 0:
                raise ValueError("PATH_FILL_AMOUNT")
            object.__setattr__(self, field, value)


def fill_window(fills, *, as_of_ms):
    if (
        not isinstance(fills, (list, tuple))
        or not 2 <= len(fills) <= 1000
        or any(not isinstance(f, PathFill) for f in fills)
        or type(as_of_ms) is not int
        or not 0 <= as_of_ms < 2**53
    ):
        raise ValueError("PATH_INPUT_BOUND")
    ordered = sorted(fills, key=lambda f: (f.t, f.identity))
    if len({f.identity for f in ordered}) != len(ordered):
        raise ValueError("PATH_DUPLICATE_FILL")
    if ordered[-1].t > as_of_ms:
        raise ValueError("PATH_FUTURE_FILL")
    legs = {}
    for f in ordered:
        if f.t in legs and legs[f.t] != f.leg:
            raise ValueError("PATH_SAME_TIME_LEG_AMBIGUITY")
        legs[f.t] = f.leg
    if ordered[0].leg != "OPEN" or ordered[-1].leg != "CLOSE":
        raise ValueError("PATH_NOT_CLOSED")
    return (
        ordered,
        (ordered[0].t + MINUTE - 1) // MINUTE * MINUTE,
        ordered[-1].t // MINUTE * MINUTE,
    )


def analyze_path(fills, bars, *, side, as_of_ms):
    """bars are oldest first and exactly cover full minutes inside fill lifetime.

    Gaps suppress metrics; they are never padded. Fees/funding intentionally excluded
    from gross path; authoritative settled net PnL must be displayed separately.
    """
    ordered, start, end = fill_window(fills, as_of_ms=as_of_ms)
    if side not in {"BUY", "SELL"}:
        raise ValueError("PATH_SIDE")
    if not isinstance(bars, (list, tuple)) or len(bars) > 10080:
        raise ValueError("PATH_HISTORY_BOUND")
    expected = max(0, (end - start) // MINUTE)
    if expected > 10080:
        raise ValueError("PATH_HISTORY_BOUND")
    if any(not isinstance(b, Bar) for b in bars):
        raise ValueError("PATH_BAR_TYPE")
    if any(b.t % MINUTE or b.t < start or b.t + MINUTE > end for b in bars):
        raise ValueError("PATH_BAR_WINDOW")
    if any(a.t >= b.t for a, b in pairwise(bars)):
        raise ValueError("PATH_BAR_ORDER")
    result = {
        "version": VERSION,
        "status": "INCOMPLETE_HISTORY",
        "execution_authorized": False,
        "basis": "GROSS_REALIZED_PLUS_REMAINING_INVENTORY_MARK_TO_MARKET",
        "excursion_interpretation": "SAMPLED_LOWER_BOUND_NOT_EXECUTABLE_PROFIT",
        "opened_ms": ordered[0].t,
        "closed_ms": ordered[-1].t,
        "held_minutes": number(Fraction(ordered[-1].t - ordered[0].t, MINUTE)),
        "expected_full_minutes": expected,
        "available_full_minutes": len(bars),
        "constant_inventory_minutes": 0,
        "fill_changing_minutes_excluded": 0,
        "ambiguous_fill_price_groups_excluded": 0,
        "mfe_gross_usdt_lower_bound": None,
        "mae_gross_usdt_lower_bound": None,
        "giveback_gross_usdt_lower_bound": None,
        "observed_peak_capture_ratio": None,
        "profitable_close_sample_fraction": None,
        "longest_no_new_peak_sample_minutes": None,
        "realized_gross_usdt": None,
        "opening_notional_usdt": None,
        "gross_return_per_held_hour": None,
    }
    direction = 1 if side == "BUY" else -1
    qty = cost = realized = peak = adverse = total_open = Fraction(0)
    index = 0

    def observe(value):
        nonlocal peak, adverse
        peak, adverse = max(peak, value), max(adverse, -value)

    def apply_until(cutoff):
        nonlocal qty, cost, realized, total_open, index
        while index < len(ordered) and ordered[index].t <= cutoff:
            f = ordered[index]
            stop = index + 1
            while stop < len(ordered) and ordered[stop].t == f.t:
                stop += 1
            group = ordered[index:stop]
            q = sum((Fraction(x.quantity) for x in group), Fraction(0))
            notional = sum(
                (Fraction(x.quantity) * Fraction(x.price) for x in group), Fraction(0)
            )
            # Timestamp ties do not prove the intragroup price sequence. Gross
            # accounting is commutative for same-leg fills; extrema are not.
            if len({x.price for x in group}) == 1:
                observe(realized + direction * (qty * Fraction(f.price) - cost))
            else:
                result["ambiguous_fill_price_groups_excluded"] += 1
            if f.leg == "OPEN":
                if index and qty == 0:
                    raise ValueError("PATH_MULTIPLE_ROUND_TRIPS")
                qty += q
                cost += notional
                total_open += notional
            else:
                if q > qty or qty <= 0:
                    raise ValueError("PATH_CLOSE_EXCEEDS_INVENTORY")
                removed_cost = cost * q / qty
                realized += direction * (notional - removed_cost)
                cost -= removed_cost
                qty -= q
            if qty == 0:
                observe(realized)
            index = stop

    complete = len(bars) == expected
    positive = samples = longest = current = 0
    close_peak = Fraction(0)
    # Even incomplete histories validate fills and closed inventory; do not emit
    # seemingly usable excursions from the surviving portion of a gapped path.
    for b in bars if complete else ():
        apply_until(b.t)
        if index < len(ordered) and ordered[index].t < b.t + MINUTE:
            result["fill_changing_minutes_excluded"] += 1
            current = 0
            continue
        if qty <= 0:
            raise ValueError("PATH_FLAT_INSIDE_EPISODE")
        for price in (b.h, b.l):
            observe(realized + direction * (qty * Fraction(price) - cost))
        value = realized + direction * (qty * Fraction(b.c) - cost)
        samples += 1
        positive += value > 0
        current = current + 1 if value <= close_peak else 0
        close_peak = max(close_peak, value)
        longest = max(longest, current)
    apply_until(ordered[-1].t)
    if qty or cost:
        raise ValueError("PATH_NOT_CLOSED")
    result.update(
        realized_gross_usdt=number(realized), opening_notional_usdt=number(total_open)
    )
    if not complete:
        return result
    result["constant_inventory_minutes"] = samples
    if not samples:
        result["status"] = "INSUFFICIENT_MINUTE_RESOLUTION"
        return result
    held_ms = ordered[-1].t - ordered[0].t
    result.update(
        status="AVAILABLE_LOWER_BOUND",
        mfe_gross_usdt_lower_bound=number(peak),
        mae_gross_usdt_lower_bound=number(adverse),
        giveback_gross_usdt_lower_bound=number(max(Fraction(0), peak - realized)),
        observed_peak_capture_ratio=number(realized / peak) if peak > 0 else None,
        profitable_close_sample_fraction=number(Fraction(positive, samples)),
        longest_no_new_peak_sample_minutes=longest,
        gross_return_per_held_hour=number(
            realized / total_open * Fraction(3600000, held_ms)
        ),
    )
    return result
