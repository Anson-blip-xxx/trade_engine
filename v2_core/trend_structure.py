"""Causal 15m structure from the existing closed-minute cache; no I/O.

This contract is separate from S3's rolling-minute indicators. Its values are
observations, not a forecast, an entry decision, or an execution permission.
All prior channels/swings exclude the newest completed 15m bar. The fixed
90-bar seed makes values stable throughout a 15m bucket despite cache rotation.
"""

import json
import re
from copy import deepcopy
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise

from v2_core.evidence import canonical, digest
from v2_core.ingress import milliseconds
from v2_core.ingress import symbol as validate_symbol
from v2_core.precision import amount, decimal_text, derived_amount
from v2_core.quant_indicators import (
    MINUTE,
    Bar,
    decimal,
    smoothed,
)

VERSION = "trend-15m-structure-v1"
TIMEFRAME_MINUTES = 15
WIDTH = TIMEFRAME_MINUTES * MINUTE
REQUIRED_BARS = 90
CONTRACT = {
    "input_interval": "1m",
    "bar_semantics": "UTC_ALIGNED_CLOSED_TIME_BARS",
    "timeframe_minutes": TIMEFRAME_MINUTES,
    "indicator_period_unit": "15m_bar",
    "ema_periods": [20, 60],
    "ema_seed": "FIRST_CLOSE",
    "atr_period": 14,
    "atr_method": "WILDER_FIRST_PERIOD_SMA_SEED",
    "history_selection": "LATEST_90_COMPLETE_BARS",
    "breakout_prior_bars": 20,
    "swing_prior_bars": 5,
    "pullback_prior_bars": 3,
    "slope_bars": 3,
    "slope_unit": "PRICE_CHANGE_PER_15M_BAR",
    "prior_windows_exclude_current": True,
    "flow_method": "SUM_TAKER_BUY_BASE_VOLUME_DIVIDED_BY_SUM_BASE_VOLUME",
    "zero_volume_flow": "UNKNOWN",
    "volume_ratio_reference": "PREVIOUS_20_COMPLETE_15M_BARS",
    "input_digest_contract": "CANONICAL_OLDEST_FIRST_OHLCV_TBV_DECIMAL_STRINGS",
    "interpretation": "OBSERVED_STRUCTURE_NOT_FORECAST",
}
_META_FIELDS = frozenset(
    {
        "version",
        "status",
        "source",
        "symbol",
        "environment",
        "closed_at_ms",
        "last_closed_at_ms",
        "input_digest",
        "input_bar_count",
        "complete_bar_count",
        "indicator_bar_count",
        "required_bars",
        "excluded_partial_minutes",
        "contract",
        "values",
    }
)
_POSITIVE_FIELDS = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "previous_open",
        "previous_high",
        "previous_low",
        "previous_close",
        "ema20",
        "ema60",
        "previous_ema20",
        "previous_ema60",
        "prior20_high",
        "prior20_low",
        "prior_swing_high",
        "prior_swing_low",
        "prior3_high",
        "prior3_low",
    }
)
_NONNEGATIVE_FIELDS = frozenset({"volume", "atr", "atr_pct", "prior20_mean_volume"})
_SIGNED_FIELDS = frozenset({"ema20_slope", "ema60_slope"})
_OPTIONAL_FIELDS = frozenset({"taker_buy_ratio", "volume_ratio"})
_BOOLEAN_FIELDS = frozenset(
    {
        "prior3_touched_ema20_long",
        "prior3_touched_ema20_short",
        "breakout_long",
        "breakout_short",
    }
)
_VALUE_FIELDS = (
    _POSITIVE_FIELDS
    | _NONNEGATIVE_FIELDS
    | _SIGNED_FIELDS
    | _OPTIONAL_FIELDS
    | _BOOLEAN_FIELDS
)


def _scope(symbol, environment, closed_at_ms):
    validate_symbol(symbol)
    milliseconds(closed_at_ms)
    if environment not in {"SANDBOX", "LIVE"}:
        raise ValueError("TREND_STRUCTURE_SCOPE")
    if closed_at_ms % MINUTE:
        raise ValueError("TREND_STRUCTURE_CLOSED_BOUNDARY")


def _encoded(value, *, preserve_nonzero=False):
    return decimal_text(
        derived_amount(
            value, rounding=ROUND_HALF_EVEN, preserve_nonzero=preserve_nonzero
        )
    )


def build_trend_structure(bars, *, symbol, environment, closed_at_ms):
    """Build from 1..1440 contiguous oldest-first, already-closed 1m bars.

    Exact OHLCV/taker-volume facts are required. Missing/gapped/stale/future
    source minutes fail. Partial 15m buckets are excluded, never traded. Fewer
    than 90 complete 15m bars return WARMUP without indicator values.
    """
    _scope(symbol, environment, closed_at_ms)
    if not isinstance(bars, (list, tuple)) or not 1 <= len(bars) <= 1440:
        raise ValueError("TREND_STRUCTURE_HISTORY_BOUND")
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        normalized, parsed_minutes, taker = [], [], {}
        numeric_cache = {}

        def parsed_number(value):
            # Repeated OHLC/taker strings are common in a minute cache. Domain
            # validation is identical for each occurrence of the exact value;
            # caching it does not skip per-bar time, OHLC or flow validation.
            if type(value) not in (str, int):
                return decimal(value)
            if value not in numeric_cache:
                numeric_cache[value] = decimal(value)
            return numeric_cache[value]

        for index, row in enumerate(bars):
            if not isinstance(row, dict) or set(row) != {
                "t",
                "o",
                "h",
                "l",
                "c",
                "v",
                "tbv",
            }:
                raise ValueError("TREND_STRUCTURE_BAR_FIELDS")
            opened = milliseconds(row["t"])
            if opened != closed_at_ms - (len(bars) - index) * MINUTE:
                raise ValueError("TREND_STRUCTURE_MINUTE_SEQUENCE")
            numbers = {
                key: parsed_number(row[key]) for key in ("o", "h", "l", "c", "v", "tbv")
            }
            buy = numbers["tbv"]
            if not 0 <= buy <= numbers["v"]:
                raise ValueError("TREND_STRUCTURE_TAKER_VOLUME")
            if not (
                Decimal("1e-18") <= numbers["l"] <= numbers["o"] <= numbers["h"]
                and numbers["l"] <= numbers["c"] <= numbers["h"]
            ):
                raise ValueError("TREND_STRUCTURE_OHLC")
            normalized.append(
                {
                    "t": opened,
                    **{key: format(value, "f") for key, value in numbers.items()},
                }
            )
            parsed_minutes.append({"t": opened, **numbers})
            taker[opened] = buy
        # Same UTC aggregation as quant_indicators.aggregate_minutes. This
        # operates on the already fully validated Decimals instead of reparsing
        # 1440 normalized string bars. Partial edge buckets never enter a Bar.
        first_complete = (-parsed_minutes[0]["t"] % WIDTH) // MINUTE
        complete = []
        for index in range(
            first_complete,
            len(parsed_minutes) - TIMEFRAME_MINUTES + 1,
            TIMEFRAME_MINUTES,
        ):
            group = parsed_minutes[index : index + TIMEFRAME_MINUTES]
            complete.append(
                Bar(
                    group[0]["t"],
                    group[0]["o"],
                    max(bar["h"] for bar in group),
                    min(bar["l"] for bar in group),
                    group[-1]["c"],
                    sum((bar["v"] for bar in group), Decimal(0)),
                )
            )
        selected = complete[-REQUIRED_BARS:]
        result = {
            "version": VERSION,
            "status": "READY" if len(selected) == REQUIRED_BARS else "WARMUP",
            "source": "BINANCE_FUTURES",
            "symbol": symbol,
            "environment": environment,
            "closed_at_ms": closed_at_ms,
            "last_closed_at_ms": complete[-1].t + WIDTH if complete else None,
            # Exact canonical serializer; schema/keys and every numeric string
            # above are already validated. Avoid a second generic traversal.
            "input_digest": digest(
                json.dumps(
                    {"bars": normalized},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            ),
            "input_bar_count": len(normalized),
            "complete_bar_count": len(complete),
            "indicator_bar_count": len(selected),
            "required_bars": REQUIRED_BARS,
            "excluded_partial_minutes": len(normalized)
            - len(complete) * TIMEFRAME_MINUTES,
            "contract": deepcopy(CONTRACT),
            "values": None,
        }
        if result["status"] == "WARMUP":
            return result
        current, previous = selected[-1], selected[-2]
        closes = [bar.c for bar in selected]
        fast, slow = (smoothed(closes, period) for period in (20, 60))
        # Match standard_series' 50-digit TR and Wilder smoothing contract,
        # without computing unused RSI/DMI/ADX series for every symbol.
        with localcontext() as indicator_context:
            indicator_context.prec = 50
            ranges = [selected[0].h - selected[0].l] + [
                max(bar.h - bar.l, abs(bar.h - before.c), abs(bar.l - before.c))
                for before, bar in pairwise(selected)
            ]
            atr = smoothed(ranges, 14, wilder=True)[-1]
        prior20, swing, pullback = selected[-21:-1], selected[-6:-1], selected[-4:-1]
        prior_high, prior_low = (
            max(bar.h for bar in prior20),
            min(bar.l for bar in prior20),
        )
        mean_volume = sum((bar.v for bar in prior20), Decimal(0)) / 20
        buy_volume = sum(
            (taker[current.t + i * MINUTE] for i in range(TIMEFRAME_MINUTES)),
            Decimal(0),
        )
        values = {
            "open": current.o,
            "high": current.h,
            "low": current.l,
            "close": current.c,
            "volume": current.v,
            "previous_open": previous.o,
            "previous_high": previous.h,
            "previous_low": previous.l,
            "previous_close": previous.c,
            "ema20": fast[-1],
            "ema60": slow[-1],
            "previous_ema20": fast[-2],
            "previous_ema60": slow[-2],
            "ema20_slope": (fast[-1] - fast[-4]) / 3,
            "ema60_slope": (slow[-1] - slow[-4]) / 3,
            "atr": atr,
            "atr_pct": atr / current.c * 100,
            "prior20_high": prior_high,
            "prior20_low": prior_low,
            "prior20_mean_volume": mean_volume,
            "prior_swing_high": max(bar.h for bar in swing),
            "prior_swing_low": min(bar.l for bar in swing),
            "prior3_high": max(bar.h for bar in pullback),
            "prior3_low": min(bar.l for bar in pullback),
        }
        result["values"] = {
            **{
                key: _encoded(
                    value, preserve_nonzero=key in _POSITIVE_FIELDS or key == "atr"
                )
                for key, value in values.items()
            },
            "taker_buy_ratio": _encoded(buy_volume / current.v) if current.v else None,
            "volume_ratio": _encoded(current.v / mean_volume) if mean_volume else None,
            "prior3_touched_ema20_long": any(
                bar.l <= ema for bar, ema in zip(pullback, fast[-4:-1], strict=True)
            ),
            "prior3_touched_ema20_short": any(
                bar.h >= ema for bar, ema in zip(pullback, fast[-4:-1], strict=True)
            ),
            "breakout_long": current.c > prior_high,
            "breakout_short": current.c < prior_low,
        }
        return validate_trend_structure(
            result, symbol=symbol, environment=environment, closed_at_ms=closed_at_ms
        )


def validate_trend_structure(structure, *, symbol, environment, closed_at_ms):
    """Validate a server-produced envelope; not authenticity proof from a client.

    The source envelope must separately bind this data and its digest. Without
    source bars this validator can check the complete schema, fixed indicator
    contract, closed time, scope, warmup counts and numeric invariants, but cannot
    independently recompute EMA/ATR or authenticate the recorded input digest.
    """
    _scope(symbol, environment, closed_at_ms)
    if not isinstance(structure, dict) or set(structure) != _META_FIELDS:
        raise ValueError("TREND_STRUCTURE_FIELDS")
    if (
        structure["version"],
        structure["source"],
        structure["symbol"],
        structure["environment"],
    ) != (VERSION, "BINANCE_FUTURES", symbol, environment):
        raise ValueError("TREND_STRUCTURE_SCOPE")
    if (
        type(structure["closed_at_ms"]) is not int
        or structure["closed_at_ms"] != closed_at_ms
    ):
        raise ValueError("TREND_STRUCTURE_TIME")
    if canonical(structure["contract"]) != canonical(CONTRACT):
        raise ValueError("TREND_STRUCTURE_CONTRACT")
    if not isinstance(structure["input_digest"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", structure["input_digest"]
    ):
        raise ValueError("TREND_STRUCTURE_DIGEST")
    count_names = (
        "input_bar_count",
        "complete_bar_count",
        "indicator_bar_count",
        "required_bars",
        "excluded_partial_minutes",
    )
    if any(type(structure[name]) is not int for name in count_names):
        raise ValueError("TREND_STRUCTURE_HISTORY")
    count = structure["input_bar_count"]
    if not 1 <= count <= 1440 or closed_at_ms < count * MINUTE:
        raise ValueError("TREND_STRUCTURE_HISTORY")
    first = closed_at_ms - count * MINUTE
    complete_count = max(0, closed_at_ms // WIDTH - (first + WIDTH - 1) // WIDTH)
    if (
        structure["complete_bar_count"] != complete_count
        or structure["indicator_bar_count"] != min(complete_count, REQUIRED_BARS)
        or structure["required_bars"] != REQUIRED_BARS
        or structure["excluded_partial_minutes"]
        != count - complete_count * TIMEFRAME_MINUTES
    ):
        raise ValueError("TREND_STRUCTURE_HISTORY")
    last_closed = closed_at_ms // WIDTH * WIDTH if complete_count else None
    if structure["last_closed_at_ms"] != last_closed or (
        last_closed is not None and type(structure["last_closed_at_ms"]) is not int
    ):
        raise ValueError("TREND_STRUCTURE_TIME")
    ready = complete_count >= REQUIRED_BARS
    if structure["status"] != ("READY" if ready else "WARMUP"):
        raise ValueError("TREND_STRUCTURE_WARMUP")
    if not ready:
        if structure["values"] is not None:
            raise ValueError("TREND_STRUCTURE_WARMUP")
        return structure
    values = structure["values"]
    if not isinstance(values, dict) or set(values) != _VALUE_FIELDS:
        raise ValueError("TREND_STRUCTURE_VALUES")
    try:
        parsed = {
            key: amount(values[key], positive=key in _POSITIVE_FIELDS)
            for key in _POSITIVE_FIELDS | _NONNEGATIVE_FIELDS | _SIGNED_FIELDS
        }
        if any(parsed[key] < 0 for key in _NONNEGATIVE_FIELDS):
            raise ValueError
        if any(type(values[key]) is not bool for key in _BOOLEAN_FIELDS):
            raise ValueError
        for prefix in ("", "previous_"):
            if not (
                parsed[prefix + "low"]
                <= parsed[prefix + "open"]
                <= parsed[prefix + "high"]
                and parsed[prefix + "low"]
                <= parsed[prefix + "close"]
                <= parsed[prefix + "high"]
            ):
                raise ValueError
        if not (
            parsed["prior20_low"]
            <= parsed["prior_swing_low"]
            <= parsed["prior3_low"]
            <= parsed["previous_low"]
            <= parsed["previous_high"]
            <= parsed["prior3_high"]
            <= parsed["prior_swing_high"]
            <= parsed["prior20_high"]
        ):
            raise ValueError
        if values["breakout_long"] != (
            parsed["close"] > parsed["prior20_high"]
        ) or values["breakout_short"] != (parsed["close"] < parsed["prior20_low"]):
            raise ValueError
        flow = values["taker_buy_ratio"]
        if (flow is None) != (parsed["volume"] == 0) or (
            flow is not None and not 0 <= amount(flow) <= 1
        ):
            raise ValueError
        ratio = values["volume_ratio"]
        if (ratio is None) != (parsed["prior20_mean_volume"] == 0) or (
            ratio is not None and amount(ratio) < 0
        ):
            raise ValueError
        # Check derived ratios without pretending rounded stored values are
        # exact source values. One storage quantum covers each rounded operand.
        with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
            quantum = Decimal("1e-18")
            if abs(
                parsed["atr_pct"] * parsed["close"] - parsed["atr"] * 100
            ) > quantum * (parsed["close"] + parsed["atr_pct"] + 101):
                raise ValueError
            if ratio is not None:
                parsed_ratio = amount(ratio)
                mean = parsed["prior20_mean_volume"]
                if abs(parsed_ratio * mean - parsed["volume"]) > quantum * (
                    parsed_ratio + mean + 1
                ):
                    raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        raise ValueError("TREND_STRUCTURE_VALUES") from None
    return structure
