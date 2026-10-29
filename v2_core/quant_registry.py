"""Closed-bar candidate plugins and shared regime/exit/risk mathematics.

No plugin can submit an order. A registry result is research evidence, not an
execution permit. Defaults enable only the two P1 entry hypotheses for research.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from itertools import pairwise

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import (
    VERSION,
    Bar,
    choppiness,
    decimal,
    encode,
    regression_last,
    smoothed,
    standard_series,
)

REGISTRY_VERSION = "quant-candidates-v1"
PLUGINS = ("donchian", "squeeze", "atr_channel", "supertrend", "vwap_reversion")
DEFAULTS = {
    "period": 14,
    "channel_period": 20,
    "band_period": 20,
    "ema_fast": 12,
    "ema_slow": 26,
    "volume_period": 20,
    "bb_multiplier": "2",
    "kc_multiplier": "1.5",
    "stop_atr": "2",
    "trail_atr": "3",
    "supertrend_atr": "3",
    "min_rvol": "1.2",
    "adx_trend": "20",
    "chop_range": "61.8",
    "max_stop_fraction": "0.08",
    "max_atr_fraction": "0.06",
    "vwap_z": "2",
    "base_score": 50,
    "rvol_score_weight": "10",
    "max_rvol_score": "30",
    "trend_regime_required": False,
    "target_r_multiple": "2",
}


def parameters(supplied=None):
    supplied = {} if supplied is None else supplied
    if not isinstance(supplied, dict) or set(supplied) - DEFAULTS.keys():
        raise ValueError("QUANT_UNKNOWN_PARAMETER")
    values = {**DEFAULTS, **supplied}
    for name, value in values.items():
        if type(value) is not type(DEFAULTS[name]):
            raise ValueError("QUANT_PARAMETER_TYPE:" + name)
        if type(value) is bool:
            continue
        if type(value) is int:
            low, high = (0, 100) if name == "base_score" else (2, 2000)
        else:
            low, high = 0, 100
        number = decimal(value)
        if not low <= number <= high:
            raise ValueError("QUANT_PARAMETER_RANGE:" + name)
    if not values["ema_fast"] < values["ema_slow"]:
        raise ValueError("QUANT_EMA_ORDER")
    if not Decimal(0) < decimal(values["max_stop_fraction"]) <= Decimal("0.12"):
        raise ValueError("QUANT_STOP_BOUND")
    if not Decimal(0) < decimal(values["max_atr_fraction"]) <= Decimal(1):
        raise ValueError("QUANT_ATR_BOUND")
    if any(
        decimal(values[k]) <= 0
        for k in (
            "stop_atr",
            "trail_atr",
            "supertrend_atr",
            "bb_multiplier",
            "kc_multiplier",
            "min_rvol",
            "vwap_z",
            "target_r_multiple",
        )
    ):
        raise ValueError("QUANT_POSITIVE_PARAMETER")
    return values


def warmup(params=None):
    p = parameters(params)
    return max(
        3 * p["ema_slow"],
        2 * p["period"] + 2,
        2 * p["band_period"] + 2,
        p["channel_period"] + 2,
        p["volume_period"] + 2,
    )


def _supertrend(bars, atr, multiplier):
    direction, upper, lower, result = 1, None, None, []
    for i, (bar, a) in enumerate(zip(bars, atr, strict=True)):
        if a is None:
            result.append(None)
            continue
        basic_upper = (bar.h + bar.l) / 2 + multiplier * a
        basic_lower = (bar.h + bar.l) / 2 - multiplier * a
        if upper is not None:
            old_upper, old_lower = upper, lower
            upper = (
                basic_upper if basic_upper < upper or bars[i - 1].c > upper else upper
            )
            lower = (
                basic_lower if basic_lower > lower or bars[i - 1].c < lower else lower
            )
            direction = (
                1 if bar.c > old_upper else -1 if bar.c < old_lower else direction
            )
        else:
            upper, lower = basic_upper, basic_lower
        result.append(direction)
    return result


def feature_frame(bars, *, timeframe_minutes, params=None):
    p = parameters(params)
    needed = warmup(p)
    if len(bars) < needed:
        return {
            "version": VERSION,
            "status": "WARMUP",
            "required": needed,
            "available": len(bars),
        }
    bars = tuple(
        bars[-needed:]
    )  # Deterministic bounded seed, independent of cache depth.
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
    width = timeframe_minutes * 60_000
    if any(not isinstance(b, Bar) or b.t % width for b in bars) or any(
        b.t != a.t + width for a, b in pairwise(bars)
    ):
        raise ValueError("QUANT_AGGREGATE_GAP")
    with localcontext() as ctx:
        ctx.prec = 50
        series = standard_series(bars, p["period"])
        closes = [b.c for b in bars]
        fast = smoothed(closes, p["ema_fast"])
        slow = smoothed(closes, p["ema_slow"])
        supertrend = _supertrend(bars, series["atr"], decimal(p["supertrend_atr"]))
        snapshots = []
        for count in (len(bars) - 1, len(bars)):
            recent = bars[:count]
            current = recent[-1]
            band = recent[-p["band_period"] :]
            mean = sum(b.c for b in band) / len(band)
            stdev = (sum((b.c - mean) ** 2 for b in band) / len(band)).sqrt()
            atr = series["atr"][count - 1]
            center = fast[count - 1]
            # KC uses EMA center and Wilder ATR; this version's choice is frozen.
            compressed = (
                mean - decimal(p["bb_multiplier"]) * stdev
                > center - decimal(p["kc_multiplier"]) * atr
                and mean + decimal(p["bb_multiplier"]) * stdev
                < center + decimal(p["kc_multiplier"]) * atr
            )
            channel = recent[-p["channel_period"] - 1 : -1]
            prior_volume = recent[-p["volume_period"] - 1 : -1]
            volume_mean = sum(b.v for b in prior_volume) / len(prior_volume)
            vwap_volume = sum(b.v for b in band)
            vwap = sum(b.c * b.v for b in band) / vwap_volume if vwap_volume else None
            ci = choppiness(recent, series["tr"][:count], p["period"])
            adx = series["adx"][count - 1]
            regime = (
                "TREND"
                if adx >= decimal(p["adx_trend"]) and ci < decimal(p["chop_range"])
                else "RANGE"
                if adx < decimal(p["adx_trend"]) and ci >= decimal(p["chop_range"])
                else "TRANSITION"
            )
            raw = {
                "close": current.c,
                "atr": atr,
                "atr_fraction": atr / current.c,
                "rsi": series["rsi"][count - 1],
                "adx": adx,
                "choppiness": ci,
                "ema_fast": fast[count - 1],
                "ema_slow": slow[count - 1],
                "donchian_upper": max(b.h for b in channel),
                "donchian_lower": min(b.l for b in channel),
                "rvol": current.v / volume_mean if volume_mean else None,
                "channel_upper": center + decimal(p["kc_multiplier"]) * atr,
                "channel_lower": center - decimal(p["kc_multiplier"]) * atr,
                "momentum": (regression_last([b.c for b in band]) - mean) / current.c,
                "vwap": vwap,
                "vwap_z": (current.c - vwap) / stdev
                if stdev and vwap is not None
                else None,
                "highest": max(b.h for b in band),
                "lowest": min(b.l for b in band),
            }
            snapshots.append(
                {
                    **{k: encode(v) for k, v in raw.items()},
                    "compressed": compressed,
                    "regime": regime,
                    "supertrend": supertrend[count - 1],
                }
            )
        payload = [b.payload() for b in bars]
        return {
            "version": VERSION,
            "status": "READY",
            "timeframe_minutes": timeframe_minutes,
            "opened_at_ms": bars[0].t,
            "closed_at_ms": bars[-1].t + width,
            "bar_count": len(bars),
            "bars_digest": digest(canonical({"bars": payload})),
            "previous": snapshots[0],
            "current": snapshots[1],
            "parameters": p,
        }


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    strategy_version: str
    family: str
    side: str | None
    reason: str
    score: int
    reference_price: str
    stop_fraction: str | None


def evaluate(frame, *, enabled=("donchian", "squeeze")):
    if (
        not isinstance(enabled, (tuple, list))
        or not enabled
        or len(set(enabled)) != len(enabled)
        or not set(enabled) <= set(PLUGINS)
    ):
        raise ValueError("QUANT_REGISTERED_PLUGINS_REQUIRED")
    if frame.get("version") != VERSION:
        raise ValueError("QUANT_FEATURE_VERSION")
    if frame["status"] != "READY":
        return {
            "version": REGISTRY_VERSION,
            "status": "WARMUP",
            "evaluations": [],
            "winner": None,
        }
    p = parameters(frame["parameters"])
    cur, old = frame["current"], frame["previous"]
    price, atr = decimal(cur["close"]), decimal(cur["atr"])
    volume = decimal(cur["rvol"]) if cur["rvol"] is not None else None
    entries = {}
    entries["donchian"] = (
        "BUY"
        if price > decimal(cur["donchian_upper"])
        and decimal(old["close"]) <= decimal(old["donchian_upper"])
        else "SELL"
        if price < decimal(cur["donchian_lower"])
        and decimal(old["close"]) >= decimal(old["donchian_lower"])
        else None
    )
    entries["squeeze"] = (
        (
            "BUY"
            if decimal(cur["momentum"]) > 0
            else "SELL"
            if decimal(cur["momentum"]) < 0
            else None
        )
        if old["compressed"] and not cur["compressed"]
        else None
    )
    entries["atr_channel"] = (
        "BUY"
        if price > decimal(cur["channel_upper"])
        and decimal(old["close"]) <= decimal(old["channel_upper"])
        else "SELL"
        if price < decimal(cur["channel_lower"])
        and decimal(old["close"]) >= decimal(old["channel_lower"])
        else None
    )
    entries["supertrend"] = (
        ("BUY" if cur["supertrend"] == 1 else "SELL")
        if cur["supertrend"] != old["supertrend"]
        else None
    )
    z, old_z = cur["vwap_z"], old["vwap_z"]
    threshold = decimal(p["vwap_z"])
    # Enter on return inside the band, not on an accelerating excursion.
    entries["vwap_reversion"] = (
        None
        if z is None or old_z is None
        else "BUY"
        if decimal(old_z) < -threshold <= decimal(z)
        else "SELL"
        if decimal(old_z) > threshold >= decimal(z)
        else None
    )
    results = []
    with localcontext() as ctx:
        ctx.prec = 50
        stop = decimal(p["stop_atr"]) * atr / price
        score = min(
            100,
            p["base_score"]
            + int(
                min(
                    decimal(p["max_rvol_score"]),
                    max(Decimal(0), (volume or 0) - 1)
                    * decimal(p["rvol_score_weight"]),
                )
            ),
        )
        for name in enabled:
            side, reason = entries[name], "CANDIDATE"
            if atr <= 0:
                reason = "ZERO_VOLATILITY"
            elif not side:
                reason = "NO_TRIGGER"
            elif stop > decimal(p["max_stop_fraction"]) or decimal(
                cur["atr_fraction"]
            ) > decimal(p["max_atr_fraction"]):
                reason = "VOLATILITY_RISK_LIMIT"
            elif volume is None or volume < decimal(p["min_rvol"]):
                reason = "INSUFFICIENT_RELATIVE_VOLUME"
            elif name == "vwap_reversion" and cur["regime"] != "RANGE":
                reason = "MEAN_REVERSION_REGIME_MISMATCH"
            elif (
                name != "vwap_reversion"
                and p["trend_regime_required"]
                and cur["regime"] == "RANGE"
            ):
                reason = "TREND_REGIME_MISMATCH"
            results.append(
                asdict(
                    Candidate(
                        name,
                        REGISTRY_VERSION,
                        "mean_reversion" if name == "vwap_reversion" else "trend",
                        side if reason == "CANDIDATE" else None,
                        reason,
                        score,
                        cur["close"],
                        encode(stop) if stop else None,
                    )
                )
            )
    actionable = [r for r in results if r["reason"] == "CANDIDATE"]
    conflict = len({r["side"] for r in actionable}) > 1
    winner = (
        None
        if conflict or not actionable
        else min(
            actionable, key=lambda r: (-r["score"], enabled.index(r["strategy_id"]))
        )
    )
    return {
        "version": REGISTRY_VERSION,
        "status": "CONFLICT" if conflict else "CANDIDATE" if winner else "IGNORED",
        "evaluations": results,
        "winner": winner,
    }


def trailing_stop(*, side, prior_stop, high_water, low_water, atr, multiplier):
    if side not in {"BUY", "SELL"}:
        raise ValueError("QUANT_EXIT_SIDE")
    prior, high, low, volatility, factor = map(
        decimal, (prior_stop, high_water, low_water, atr, multiplier)
    )
    if not 0 < low <= high or prior <= 0 or volatility <= 0 or factor <= 0:
        raise ValueError("QUANT_EXIT_FACTS")
    return encode(
        max(prior, high - factor * volatility)
        if side == "BUY"
        else min(prior, low + factor * volatility)
    )
