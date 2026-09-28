"""Versioned price-dimension indicators without legacy six-decimal rounding.

Keep the original EMA seed/window and ATR sample semantics. Only precision
changes here; legacy archived frames must continue to replay byte-for-byte.
"""

from decimal import ROUND_HALF_EVEN, Decimal, localcontext


def price_features(bars):
    """Validated decimal-string candles, oldest first; no I/O or price floors."""
    with localcontext() as context:
        context.prec = 50
        closes = [Decimal(bar["c"]) for bar in bars]

        def encoded(value):
            quantized = value.quantize(Decimal("1e-18"), rounding=ROUND_HALF_EVEN)
            if value != 0 and quantized == 0:
                raise ValueError("MARKET_INDICATOR_BELOW_PRECISION")
            return format(quantized.normalize(), "f")

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
