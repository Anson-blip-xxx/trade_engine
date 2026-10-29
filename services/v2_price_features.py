"""Versioned price-dimension indicators without legacy six-decimal rounding.

price_features preserves original v2 EMA/ATR semantics for exact replay.
price_features_v3 explicitly opts into latest-range ATR on rolling 1m input.
price_features_v4 also publishes the latest close's position in that window.
None of these functions aggregate real hourly or daily bars.
"""

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext

from v2_core.precision import decimal_text, derived_amount


def price_features(bars):
    """Validated decimal-string candles, oldest first; no I/O or price floors."""
    # Preserve the v2 indicator algorithm's 50-digit working precision, but
    # do not inherit another task's traps/rounding mode.
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        closes = [Decimal(bar["c"]) for bar in bars]

        def encoded(value):
            quantized = derived_amount(value, rounding=ROUND_HALF_EVEN)
            if value != 0 and quantized == 0:
                raise ValueError("MARKET_INDICATOR_BELOW_PRECISION")
            return decimal_text(quantized)

        def ema(period):
            if len(closes) < period:
                return closes[-1]
            weight = Decimal(2) / (period + 1)
            value = sum(closes[:period]) / period
            for close in closes[period:]:
                value = close * weight + value * (1 - weight)
            return value

        ranges = [
            max(
                Decimal(bars[i]["h"]) - Decimal(bars[i]["l"]),
                abs(Decimal(bars[i]["h"]) - closes[i - 1]),
                abs(Decimal(bars[i]["l"]) - closes[i - 1]),
            )
            for i in range(1, min(len(bars), 15))
        ]
        atr = sum(ranges) / len(ranges) if ranges else Decimal(0)
        return {
            "close": encoded(closes[-1]),
            "high": encoded(max(Decimal(bar["h"]) for bar in bars)),
            "low": encoded(min(Decimal(bar["l"]) for bar in bars)),
            "ema20": encoded(ema(20)),
            "ema60": encoded(ema(60)),
            "atr": encoded(atr),
            "atr_pct": encoded(atr / closes[-1] * 100),
        }


def price_features_v3(bars):
    """Rolling *one-minute* features; not hourly or daily candle indicators.

    The EMA seed/fallback is deliberately the same as v2. ATR is the arithmetic
    mean of the latest 14 true ranges, not the oldest 14 ranges. Callers must
    supply only already-closed bars; timestamps are checked for 1m continuity.
    Keep this separate from price_features so historical v2 replay is immutable.
    """
    if not isinstance(bars, (list, tuple)) or not 2 <= len(bars) <= 1440:
        raise ValueError("MARKET_INDICATOR_HISTORY_BOUND")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        parsed = []
        previous = None
        for bar in bars:
            if not isinstance(bar, dict) or not {"t", "h", "l", "c"} <= bar.keys():
                raise ValueError("MARKET_INDICATOR_BAR_FIELDS")
            opened = bar["t"]
            if (
                type(opened) is not int
                or not 0 <= opened <= 2**53 - 1
                or opened % 60000
                or (previous is not None and opened != previous + 60000)
            ):
                raise ValueError("MARKET_INDICATOR_MINUTE_SEQUENCE")
            values = {}
            for key in ("h", "l", "c"):
                value = bar[key]
                if type(value) not in (str, int) or len(str(value)) > 80:
                    raise ValueError("MARKET_INDICATOR_DECIMAL_REQUIRED")
                try:
                    number = Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError("MARKET_INDICATOR_DECIMAL_INVALID") from exc
                if not number.is_finite() or not 0 < number < Decimal("1e20"):
                    raise ValueError("MARKET_INDICATOR_DECIMAL_RANGE")
                values[key] = number
            if not values["l"] <= values["c"] <= values["h"]:
                raise ValueError("MARKET_INDICATOR_OHLC")
            parsed.append(values)
            previous = opened

        closes = [bar["c"] for bar in parsed]

        def encoded(value):
            quantized = derived_amount(value, rounding=ROUND_HALF_EVEN)
            if value != 0 and quantized == 0:
                raise ValueError("MARKET_INDICATOR_BELOW_PRECISION")
            return decimal_text(quantized)

        def ema(period):
            if len(closes) < period:
                return closes[-1]
            weight = Decimal(2) / (period + 1)
            value = sum(closes[:period]) / period
            for close in closes[period:]:
                value = close * weight + value * (1 - weight)
            return value

        ranges = [
            max(
                parsed[i]["h"] - parsed[i]["l"],
                abs(parsed[i]["h"] - parsed[i - 1]["c"]),
                abs(parsed[i]["l"] - parsed[i - 1]["c"]),
            )
            for i in range(max(1, len(parsed) - 14), len(parsed))
        ]
        atr = sum(ranges) / len(ranges)
        return {
            "close": encoded(closes[-1]),
            "high": encoded(max(bar["h"] for bar in parsed)),
            "low": encoded(min(bar["l"] for bar in parsed)),
            "ema20": encoded(ema(20)),
            "ema60": encoded(ema(60)),
            "atr": encoded(atr),
            "atr_pct": encoded(atr / closes[-1] * 100),
        }


def price_features_v4(bars):
    """V3 price features plus a causal 0..100 rolling-window close position.

    Require 15 closed minute bars so the latest 14 true ranges are available.
    Close position uses the original decimal prices (not rounded feature output):
    100 * (latest close - window low) / (window high - window low). A flat
    window is neutral at 50; an actual close at the low is valid zero.
    build_frame supplies and validates the historical closed-at boundary.
    """
    if not isinstance(bars, (list, tuple)) or not 15 <= len(bars) <= 1440:
        raise ValueError("MARKET_INDICATOR_HISTORY_BOUND")
    result = price_features_v3(bars)
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        high = max(Decimal(bar["h"]) for bar in bars)
        low = min(Decimal(bar["l"]) for bar in bars)
        close = Decimal(bars[-1]["c"])
        position = (close - low) / (high - low) * 100 if high != low else Decimal(50)
        result["close_pos"] = decimal_text(
            derived_amount(position, rounding=ROUND_HALF_EVEN)
        )
    return result
