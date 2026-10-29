"""Causal true time-bar adapters for research, distinct from rolling windows.

No I/O or clock. Callers supply the historical decision cutoff. The underlying
standard definitions are shared with quant_indicators, not silently substituted
for s3's versioned rolling-minute features.
"""

from decimal import ROUND_HALF_EVEN, Context, localcontext

from v2_core.precision import decimal_text, derived_amount
from v2_core.quant_indicators import (
    MINUTE,
    Bar,
    aggregate_minutes,
    smoothed,
    standard_series,
)

VERSION = "utc-closed-time-bars-v1"
TIMEFRAMES = (1, 5, 15, 30, 60, 240, 1440)


def _boundary(timeframe_minutes, as_of_ms):
    if type(timeframe_minutes) is not int or timeframe_minutes not in TIMEFRAMES:
        raise ValueError("TIMEFRAME_UNSUPPORTED")
    if type(as_of_ms) is not int or not 0 <= as_of_ms <= 2**53 - 1:
        raise ValueError("TIMEFRAME_AS_OF_INVALID")
    return timeframe_minutes * MINUTE


def _closed_rows(rows, *, width, as_of_ms, allow_empty=False):
    if (
        not isinstance(rows, (list, tuple))
        or not (0 if allow_empty else 1) <= len(rows) <= 100_000
    ):
        raise ValueError("TIMEFRAME_HISTORY_BOUND")
    result, ignored, previous = [], 0, None
    for row in rows:
        if not isinstance(row, dict) or "t" not in row:
            raise ValueError("TIMEFRAME_BAR_FIELDS")
        t = row["t"]
        if type(t) is not int or not 0 <= t <= 2**53 - 1 or t % width:
            raise ValueError("TIMEFRAME_BAR_ALIGNMENT")
        if previous is not None and t <= previous:
            raise ValueError("TIMEFRAME_BAR_ORDER")
        previous = t
        if t + width > as_of_ms:
            # Even the OHLC values of current/future candles cannot influence a
            # historical decision or poison it with an invalid future price.
            ignored += 1
            continue
        if not {"t", "o", "h", "l", "c", "v"} <= row.keys():
            raise ValueError("TIMEFRAME_BAR_FIELDS")
        bar = Bar(**{key: row[key] for key in ("t", "o", "h", "l", "c", "v")})
        if result and bar.t != result[-1].t + width:
            raise ValueError("TIMEFRAME_HISTORY_GAP")
        result.append(bar)
    return result, ignored


def aggregate_closed_bars(bars, *, timeframe_minutes, as_of_ms):
    """Aggregate contiguous oldest-first 1m OHLCV into fully closed UTC bars.

    Partial leading/trailing buckets are excluded and counted. Missing closed
    minutes fail, never carry forward. Future/current bars are ignored. Source
    history must extend through the most recent fully closed source minute.
    Taker volume is not inferred or propagated: output uses the Bar OHLCV schema.
    """
    width = _boundary(timeframe_minutes, as_of_ms)
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        closed, ignored = _closed_rows(bars, width=MINUTE, as_of_ms=as_of_ms)
        source_cutoff = as_of_ms // MINUTE * MINUTE
        if closed and closed[-1].t + MINUTE != source_cutoff:
            raise ValueError("TIMEFRAME_HISTORY_STALE")
        aggregated = (
            aggregate_minutes(
                [bar.payload() for bar in closed],
                timeframe_minutes=timeframe_minutes,
                closed_at_ms=source_cutoff,
            )
            if closed
            else ()
        )
        return {
            "bars": [bar.payload() for bar in aggregated],
            "metadata": {
                "version": VERSION,
                "input_interval": "1m",
                "timeframe_minutes": timeframe_minutes,
                "bar_semantics": "UTC_ALIGNED_CLOSED_TIME_BARS",
                "as_of_ms": as_of_ms,
                "last_closed_at": aggregated[-1].t + width if aggregated else None,
                "complete_bar_count": len(aggregated),
                "excluded_partial_minutes": len(closed)
                - len(aggregated) * timeframe_minutes,
                "ignored_unclosed_minutes": ignored,
            },
        }


def closed_bar_indicators(
    bars, *, timeframe_minutes, as_of_ms, ema_periods=(20, 60), atr_period=14
):
    """EMA/Wilder ATR on true closed bars; warmup never emits tradeable values.

    EMA seeds at the first close (same quant research convention); ATR seeds
    from the first period's true ranges, with first range equal to high-low.
    Every period is measured in the requested time bars, not underlying minutes.
    """
    width = _boundary(timeframe_minutes, as_of_ms)
    if (
        not isinstance(ema_periods, (list, tuple))
        or not 1 <= len(ema_periods) <= 8
        or any(type(p) is not int or not 1 <= p <= 2000 for p in ema_periods)
        or len(set(ema_periods)) != len(ema_periods)
        or type(atr_period) is not int
        or not 1 <= atr_period <= 2000
    ):
        raise ValueError("TIMEFRAME_PERIOD_INVALID")
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        closed, ignored = _closed_rows(
            bars, width=width, as_of_ms=as_of_ms, allow_empty=True
        )
        if closed and closed[-1].t + width != as_of_ms // width * width:
            raise ValueError("TIMEFRAME_HISTORY_STALE")
        required = max(*ema_periods, atr_period)
        result = {
            "version": VERSION,
            "status": "READY" if len(closed) >= required else "WARMUP",
            "timeframe_minutes": timeframe_minutes,
            "as_of_ms": as_of_ms,
            "last_closed_at": closed[-1].t + width if closed else None,
            "bar_count": len(closed),
            "required_bars": required,
            "ignored_unclosed_bars": ignored,
            "ema_seed": "FIRST_CLOSE",
            "atr_method": "WILDER_FIRST_PERIOD_SMA_SEED",
            "values": None,
        }
        if result["status"] != "READY":
            return result

        def encoded(value):
            return decimal_text(
                derived_amount(value, rounding=ROUND_HALF_EVEN, preserve_nonzero=True)
            )

        closes = [bar.c for bar in closed]
        atr = standard_series(closed, period=atr_period)["atr"][-1]
        result["values"] = {
            **{
                f"ema{period}": encoded(smoothed(closes, period)[-1])
                for period in ema_periods
            },
            "atr": encoded(atr),
            "atr_pct": encoded(atr / closes[-1] * 100),
            "close": encoded(closes[-1]),
        }
        return result
