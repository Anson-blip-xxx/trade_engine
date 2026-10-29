"""Bounded fixed-clock precursor descriptors, separate from future labels.

Reuse already archived LIVE hourly candles. No network, database, order port,
or model calls. Current-catalog sampling remains survivorship-limited and the
archive does not prove historical receipt time: this is NOT a live backtest.
"""

from collections import Counter
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from v2_core.daily_movers import DAY, MINUTE, _valid_symbol
from v2_core.evidence import canonical, digest
from v2_core.move_precursors import analyze_move_precursor

VERSION = "daily-fixed-clock-precursors-v1"
SOURCE = "BINANCE_USDM_LIVE"
HOUR = 60 * MINUTE
CUTOFF_HOURS = (6, 12, 18)


def _price(value):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError("PRECURSOR_PRICE_INVALID")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("PRECURSOR_PRICE_INVALID") from None
    if not number.is_finite() or not Decimal("1e-30") <= number <= Decimal("1e30"):
        raise ValueError("PRECURSOR_PRICE_INVALID")
    return number


def _text(value):
    return format(value.quantize(Decimal("0.00000001")), "f")


def forward_labels(rows, *, symbol, cutoff_ms, side, as_of_ms):
    """Six closed hourly observations AFTER the descriptor; never feature input.

    Close-to-close excursion is a sampled price-path descriptor, not executable
    PnL, an intrabar ordering claim, or permission to increase holding risk.
    """
    if (
        side not in {"BUY", "SELL"}
        or not _valid_symbol(symbol)
        or any(type(t) is not int for t in (cutoff_ms, as_of_ms))
        or not HOUR <= cutoff_ms < 2**53 - 6 * HOUR
        or not 0 <= as_of_ms < 2**53
        or cutoff_ms % HOUR
    ):
        raise ValueError("PRECURSOR_LABEL_CONFIGURATION_INVALID")
    end = cutoff_ms + 6 * HOUR
    unknown = {"status": "UNKNOWN", "reason": "FORWARD_WINDOW_UNAVAILABLE"}
    if as_of_ms < end or not isinstance(rows, (list, tuple)) or len(rows) > 48:
        return unknown
    chosen = {}
    try:
        for row in rows:
            if not isinstance(row, dict) or type(row.get("t")) is not int:
                raise ValueError()
            t = row["t"]
            if not cutoff_ms - HOUR <= t < end:
                continue
            if t in chosen or t % HOUR:
                raise ValueError()
            if (
                row.get("symbol", symbol) != symbol
                or row.get("market_source", SOURCE) != SOURCE
                or row.get("environment", "LIVE") != "LIVE"
            ):
                raise ValueError()
            o, h, low, close = (_price(row[k]) for k in ("o", "h", "l", "c"))
            if not low <= min(o, close) <= max(o, close) <= h:
                raise ValueError()
            chosen[t] = close
        if set(chosen) != set(range(cutoff_ms - HOUR, end, HOUR)):
            return unknown
        base = chosen[cutoff_ms - HOUR]
        sign = 1 if side == "BUY" else -1
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            returns = [
                (chosen[t] / base - 1) * 100 * sign for t in range(cutoff_ms, end, HOUR)
            ]
            return {
                "status": "OBSERVED",
                "basis": "LIVE_HOURLY_CLOSE_PRICE_RETURN_NOT_PNL",
                "start_ms": cutoff_ms,
                "end_ms": end,
                "horizon_hours": 6,
                "directional_end_return_pct": _text(returns[-1]),
                "favorable_close_excursion_pct": _text(max([Decimal(0), *returns])),
                "adverse_close_excursion_pct": _text(min([Decimal(0), *returns])),
                "input_digest": digest(
                    canonical(
                        {
                            "version": VERSION,
                            "source": SOURCE,
                            "symbol": symbol,
                            "side": side,
                            "cutoff_ms": cutoff_ms,
                            "closes": [[t, str(chosen[t])] for t in sorted(chosen)],
                        }
                    )
                ),
                "execution_authorized": False,
            }
    except (ValueError, KeyError, ArithmeticError):
        return unknown


def build_precursor_review(
    *, window, catalog, samples, ranking, as_of_ms, cohort_size=20
):
    """Select controls BEFORE considering return/rank, including unknown cases.

    Both directions are assessed at the same fixed clocks for all selected
    symbols. TOP20 is an additional hindsight diagnostic set, not the control
    universe, and must not be used as a predictive success-rate denominator.
    """
    if type(cohort_size) is not int or not 1 <= cohort_size <= 40:
        raise ValueError("PRECURSOR_COHORT_BOUND")
    if (
        not isinstance(window, dict)
        or not isinstance(catalog, list)
        or len(catalog) > 3000
        or not isinstance(samples, dict)
        or len(samples) > 3000
        or not isinstance(ranking, dict)
        or ranking.get("market_source") != SOURCE
    ):
        raise ValueError("PRECURSOR_INPUT_INVALID")
    start, end = window.get("start_ms"), window.get("end_ms")
    if (
        any(type(t) is not int for t in (start, end, as_of_ms))
        or not 0 <= start < end <= as_of_ms < 2**53
        or end - start != DAY
        or (start + 8 * HOUR) % DAY
        or ranking.get("start_ms") != start
        or ranking.get("end_ms") != end
    ):
        raise ValueError("PRECURSOR_WINDOW_INVALID")
    names = [item.get("symbol") if isinstance(item, dict) else None for item in catalog]
    if any(not _valid_symbol(s) for s in names) or len(names) != len(set(names)):
        raise ValueError("PRECURSOR_CATALOG_INVALID")
    cohort = sorted(
        names,
        key=lambda symbol: (
            digest(
                canonical({"version": VERSION, "start_ms": start, "symbol": symbol})
            ),
            symbol,
        ),
    )[:cohort_size]
    top = []
    for group in ("winners", "losers"):
        items = ranking.get(group, [])
        if not isinstance(items, list) or len(items) > 10:
            raise ValueError("PRECURSOR_TOP_BOUND")
        for item in items:
            symbol = item.get("symbol") if isinstance(item, dict) else None
            if symbol not in names or symbol in top:
                raise ValueError("PRECURSOR_TOP_IDENTITY")
            top.append(symbol)
    selected = sorted(set(cohort) | set(top))
    cases = []
    for symbol in selected:
        record = samples.get(symbol)
        rows = record.get("hourly") if isinstance(record, dict) else None
        for hour in CUTOFF_HOURS:
            cutoff = start + hour * HOUR
            for side in ("BUY", "SELL"):
                try:
                    assessment = analyze_move_precursor(
                        rows,
                        symbol=symbol,
                        side=side,
                        cutoff_ms=cutoff,
                        interval_minutes=60,
                        window_bars=3,
                        market_source=SOURCE,
                    )
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    assessment = {"status": "UNKNOWN", "reason": "INPUT_UNAVAILABLE"}
                cases.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "cutoff_ms": cutoff,
                        "fixed_cohort": symbol in cohort,
                        "top_mover_diagnostic": symbol in top,
                        "features_at_cutoff": {
                            key: assessment[key]
                            for key in ("status", "reason", "features", "input_digest")
                            if key in assessment
                        },
                        "future_labels_not_features": forward_labels(
                            rows,
                            symbol=symbol,
                            cutoff_ms=cutoff,
                            side=side,
                            as_of_ms=as_of_ms,
                        ),
                    }
                )
    feature_counts = Counter(case["features_at_cutoff"]["status"] for case in cases)
    return {
        "version": VERSION,
        "status": "DESCRIPTIVE_RESEARCH_ONLY",
        "source": SOURCE,
        "catalog_basis": ranking.get("universe_basis", "UNKNOWN"),
        "selection_basis": "FIXED_DAY_SYMBOL_HASH_WITHOUT_RETURN_OR_COMPLETENESS_FILTER",
        "fixed_cohort": cohort,
        "top_mover_diagnostics": top,
        "cutoff_hours_utc8": list(CUTOFF_HOURS),
        "baseline_hours": 3,
        "recent_hours": 3,
        "feature_status_counts": dict(feature_counts),
        "cases": cases,
        "catalog_selection_digest": digest(
            canonical(
                {
                    "version": VERSION,
                    "start_ms": start,
                    "catalog": sorted(names),
                    "cohort_size": cohort_size,
                }
            )
        ),
        "unobserved_mechanisms": {
            "spot_accumulation_or_distribution": "UNKNOWN_NO_SPOT_TAKER_FLOW_HISTORY",
            "inventory_absorption": "UNKNOWN_NO_SEQUENCE_OF_BOOK_AND_EXECUTIONS",
            "leverage_squeeze": "UNKNOWN_NO_ALIGNED_OI_FUNDING_LIQUIDATION_HISTORY",
            "participant_intent": "NOT_IDENTIFIABLE_FROM_PUBLIC_OHLCV",
        },
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "limitations": [
            "Historical candle receipt time is not proven; not a causal backtest.",
            "Current catalog may omit delisted symbols; this sample is survivorship-limited.",
            "Fixed-clock descriptors are not early-warning triggers or calibrated probabilities.",
            "TOP20 diagnostic cases cannot establish prediction hit-rate or false-positive rate.",
            "BUY/SELL and adjacent cutoffs overlap; cases are not independent samples.",
            "Futures OHLCV cannot establish spot net capital inflow, maker intent or manipulation.",
            "Future price returns are separate labels, exclude fees/funding and are not strategy PnL.",
        ],
    }
