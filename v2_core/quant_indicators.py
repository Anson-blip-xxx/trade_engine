"""Versioned closed-bar mathematics. No clocks, credentials, I/O or legacy edits.

Prices are Decimal; ATR/RSI/DMI use Wilder smoothing, EMA seeds with the first
close. Warm-up is explicit. A 15-minute bar is never a rolling 15-minute window.
This is an independent implementation of standard mathematical definitions,
not a port of the third-party strategy snippets reviewed in docs/research.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

VERSION = "quant-closed-bars-v1"
MINUTE = 60_000


def decimal(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError("QUANT_DECIMAL_REQUIRED")
    if isinstance(value, str) and len(value) > 128:
        raise ValueError("QUANT_DECIMAL_RANGE")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise ValueError("QUANT_DECIMAL_INVALID") from exc
    if (
        not result.is_finite()
        or abs(result) > Decimal("1e20")
        or abs(result.as_tuple().exponent) > 200
        or len(result.as_tuple().digits) > 128
    ):
        raise ValueError("QUANT_DECIMAL_RANGE")
    return result


def encode(value):
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return format(value.quantize(Decimal("1e-18")).normalize(), "f")


@dataclass(frozen=True)
class Bar:
    t: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: Decimal

    def __post_init__(self):
        if type(self.t) is not int or not 0 <= self.t <= 2**53 - 1:
            raise ValueError("QUANT_BAR_TIME")
        for field in ("o", "h", "l", "c", "v"):
            object.__setattr__(self, field, decimal(getattr(self, field)))
        if not (
            Decimal("1e-18") <= self.l <= self.o <= self.h
            and self.l <= self.c <= self.h
        ):
            raise ValueError("QUANT_BAR_PRICES")
        if self.v < 0:
            raise ValueError("QUANT_BAR_VOLUME")

    def payload(self):
        return {k: v if k == "t" else format(v, "f") for k, v in asdict(self).items()}


def aggregate_minutes(rows, *, timeframe_minutes, closed_at_ms):
    """Input oldest first, contiguous, closed 1m bars; drop partial edge buckets.

    Missing, duplicate, out-of-order and future bars are errors, not carry-forward
    prices. UTC epoch alignment is independent of display/session timezones.
    """
    if type(timeframe_minutes) is not int or timeframe_minutes not in (
        1,
        5,
        15,
        30,
        60,
        240,
        1440,
    ):
        raise ValueError("QUANT_TIMEFRAME_UNSUPPORTED")
    if type(closed_at_ms) is not int or closed_at_ms < 0 or closed_at_ms % MINUTE:
        raise ValueError("QUANT_CLOSED_BOUNDARY")
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 100_000:
        raise ValueError("QUANT_HISTORY_BOUND")
    bars = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not {"t", "o", "h", "l", "c", "v"} <= row.keys()
        ):
            raise ValueError("QUANT_BAR_FIELDS")
        bar = Bar(**{k: row[k] for k in ("t", "o", "h", "l", "c", "v")})
        if bar.t % MINUTE or bar.t + MINUTE > closed_at_ms:
            raise ValueError("QUANT_UNCLOSED_BAR")
        if bars and bar.t != bars[-1].t + MINUTE:
            raise ValueError("QUANT_HISTORY_GAP")
        bars.append(bar)
    if bars[-1].t + MINUTE != closed_at_ms:
        raise ValueError("QUANT_HISTORY_STALE")
    width = timeframe_minutes * MINUTE
    start = (-bars[0].t % width) // MINUTE
    result = []
    for index in range(start, len(bars) - timeframe_minutes + 1, timeframe_minutes):
        group = bars[index : index + timeframe_minutes]
        result.append(
            Bar(
                group[0].t,
                group[0].o,
                max(b.h for b in group),
                min(b.l for b in group),
                group[-1].c,
                sum((b.v for b in group), Decimal(0)),
            )
        )
    return tuple(result)


def smoothed(values, period, *, wilder=False):
    if type(period) is not int or not 1 <= period <= 2000:
        raise ValueError("QUANT_PERIOD_RANGE")
    result, ready, previous = [], [], None
    with localcontext() as ctx:
        ctx.prec = 50
        alpha = Decimal(1) / period if wilder else Decimal(2) / (period + 1)
        for value in values:
            if value is None:
                result.append(None)
                continue
            value = decimal(value)
            if previous is None and wilder:
                ready.append(value)
                if len(ready) < period:
                    result.append(None)
                    continue
                previous = sum(ready) / period
            elif previous is None:
                previous = value
            else:
                previous += alpha * (value - previous)
            result.append(previous)
    return result


def standard_series(bars, period=14):
    if not bars or not all(isinstance(b, Bar) for b in bars):
        raise ValueError("QUANT_TYPED_BARS_REQUIRED")
    with localcontext() as ctx:
        ctx.prec = 50
        tr, plus, minus, gains, losses = [], [], [], [None], [None]
        for i, bar in enumerate(bars):
            if i == 0:
                tr.append(bar.h - bar.l)
                plus.append(Decimal(0))
                minus.append(Decimal(0))
                continue
            before = bars[i - 1]
            tr.append(max(bar.h - bar.l, abs(bar.h - before.c), abs(bar.l - before.c)))
            up, down, change = bar.h - before.h, before.l - bar.l, bar.c - before.c
            plus.append(max(up, Decimal(0)) if up > down else Decimal(0))
            minus.append(max(down, Decimal(0)) if down > up else Decimal(0))
            gains.append(max(change, Decimal(0)))
            losses.append(max(-change, Decimal(0)))
        atr = smoothed(tr, period, wilder=True)
        p = smoothed(plus, period, wilder=True)
        m = smoothed(minus, period, wilder=True)
        gain = smoothed(gains, period, wilder=True)
        loss = smoothed(losses, period, wilder=True)
        di_plus, di_minus, dx, rsi = [], [], [], []
        for a, p1, m1, g, l in zip(atr, p, m, gain, loss, strict=True):
            dp = None if a is None else (100 * p1 / a if a else Decimal(0))
            dm = None if a is None else (100 * m1 / a if a else Decimal(0))
            di_plus.append(dp)
            di_minus.append(dm)
            dx.append(
                None
                if dp is None
                else (100 * abs(dp - dm) / (dp + dm) if dp + dm else Decimal(0))
            )
            rsi.append(
                None
                if g is None
                else (
                    Decimal(50)
                    if g == l == 0
                    else Decimal(100)
                    if not l
                    else 100 - 100 / (1 + g / l)
                )
            )
        return {
            "tr": tr,
            "atr": atr,
            "rsi": rsi,
            "di_plus": di_plus,
            "di_minus": di_minus,
            "adx": smoothed(dx, period, wilder=True),
        }


def choppiness(bars, true_ranges, period):
    if len(bars) < period or period <= 1:
        return None
    span = max(b.h for b in bars[-period:]) - min(b.l for b in bars[-period:])
    total = sum(true_ranges[-period:])
    if not span or not total:
        return Decimal(100)
    with localcontext() as ctx:
        ctx.prec = 50
        return max(
            Decimal(0),
            min(Decimal(100), 100 * (total / span).log10() / Decimal(period).log10()),
        )


def regression_last(values):
    n = len(values)
    if n < 2:
        raise ValueError("QUANT_REGRESSION_WARMUP")
    with localcontext() as ctx:
        ctx.prec = 50
        mean = sum(values) / n
        center = Decimal(n - 1) / 2
        slope = sum(
            (Decimal(i) - center) * (value - mean) for i, value in enumerate(values)
        ) / sum((Decimal(i) - center) ** 2 for i in range(n))
        return mean + slope * center
