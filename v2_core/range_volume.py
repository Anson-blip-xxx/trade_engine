"""Independent closed-bar range/volume hypotheses; no I/O or order authority.

Inspired by public research topics, not a port or equivalence claim for any
TradingView script. All reference levels exclude the signal bar. ATR here is
the arithmetic mean of prior true ranges, intentionally not Wilder ATR.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext
from itertools import pairwise

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal, encode

VERSION = "range-volume-research-v1"
VARIANTS = ("baseline", "range", "volume", "combined")


@dataclass(frozen=True)
class RangeVolumePolicy:
    range_bars: int = 20
    atr_bars: int = 14
    volume_bars: int = 20
    max_width_atr: str = "6"
    max_efficiency: str = "0.35"
    breakout_buffer_atr: str = "0.10"
    max_extension_atr: str = "1.5"
    min_relative_volume: str = "1.5"
    min_close_strength: str = "0.65"

    def __post_init__(self):
        for name in ("range_bars", "atr_bars", "volume_bars"):
            if (
                type(getattr(self, name)) is not int
                or not 2 <= getattr(self, name) <= 200
            ):
                raise ValueError("RANGE_VOLUME_WINDOW")
        for name, value in asdict(self).items():
            if name.endswith("_bars"):
                continue
            if type(value) is not str or not 0 <= decimal(value) <= 100:
                raise ValueError("RANGE_VOLUME_PARAMETER")
        if (
            not 0 < decimal(self.max_efficiency) <= 1
            or not 0 < decimal(self.min_close_strength) <= 1
            or decimal(self.max_width_atr) <= 0
            or decimal(self.min_relative_volume) <= 0
            or not decimal(self.breakout_buffer_atr) < decimal(self.max_extension_atr)
        ):
            raise ValueError("RANGE_VOLUME_BOUND")

    @property
    def required(self):
        return max(self.range_bars, self.atr_bars + 1, self.volume_bars) + 1


def assess(bars, *, timeframe_minutes, policy=None):
    policy = RangeVolumePolicy() if policy is None else policy
    if not isinstance(policy, RangeVolumePolicy):
        raise TypeError("RANGE_VOLUME_POLICY")
    if type(timeframe_minutes) is not int or timeframe_minutes not in (
        1,
        5,
        15,
        30,
        60,
        240,
    ):
        raise ValueError("RANGE_VOLUME_TIMEFRAME")
    width = timeframe_minutes * 60000
    if (
        not isinstance(bars, (list, tuple))
        or not 1 <= len(bars) <= 100000
        or any(not isinstance(b, Bar) or b.t % width for b in bars)
        or any(b.t - a.t != width for a, b in pairwise(bars))
    ):
        raise ValueError("RANGE_VOLUME_HISTORY")
    if len(bars) < policy.required:
        return {"version": VERSION, "status": "WARMUP", "required": policy.required}
    selected = bars[-policy.required :]
    prior, current = selected[:-1], selected[-1]
    with localcontext() as ctx:
        ctx.prec = 50
        window = prior[-policy.range_bars :]
        high, low = max(b.h for b in window), min(b.l for b in window)
        tr_input = prior[-policy.atr_bars - 1 :]
        atr = (
            sum(
                (
                    max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c))
                    for a, b in pairwise(tr_input)
                ),
                Decimal(0),
            )
            / policy.atr_bars
        )
        path = sum((abs(b.c - a.c) for a, b in pairwise(window)), Decimal(0))
        efficiency = abs(window[-1].c - window[0].c) / path if path else Decimal(0)
        mean_volume = (
            sum((b.v for b in prior[-policy.volume_bars :]), Decimal(0))
            / policy.volume_bars
        )
        rvol = current.v / mean_volume if mean_volume else None
        side = "BUY" if current.c > high else "SELL" if current.c < low else None
        extension = (
            ((current.c - high) if side == "BUY" else (low - current.c)) / atr
            if side and atr
            else None
        )
        strength = (
            ((current.c - current.l) if side == "BUY" else (current.h - current.c))
            / (current.h - current.l)
            if side and current.h > current.l
            else None
        )
        range_ok = bool(
            side
            and atr > 0
            and (high - low) / atr <= decimal(policy.max_width_atr)
            and efficiency <= decimal(policy.max_efficiency)
            and decimal(policy.breakout_buffer_atr)
            <= extension
            <= decimal(policy.max_extension_atr)
        )
        volume_ok = bool(
            side
            and rvol is not None
            and strength is not None
            and rvol >= decimal(policy.min_relative_volume)
            and strength >= decimal(policy.min_close_strength)
        )
        return {
            "version": VERSION,
            "status": "READY",
            "closed_at_ms": current.t + width,
            "input_digest": digest(canonical({"bars": [b.payload() for b in selected]})),
            "side": side,
            "range_pass": range_ok,
            "volume_pass": volume_ok,
            "facts": {
                "range_high": encode(high),
                "range_low": encode(low),
                "prior_mean_tr": encode(atr),
                "prior_efficiency": encode(efficiency),
                "width_atr": encode((high - low) / atr) if atr else None,
                "relative_volume": encode(rvol),
                "close_strength": encode(strength),
                "extension_atr": encode(extension),
            },
        }


def gate(bars, side, *, timeframe_minutes, variant, policy=None):
    if variant not in VARIANTS or side not in ("BUY", "SELL"):
        raise ValueError("RANGE_VOLUME_VARIANT")
    frame = assess(bars, timeframe_minutes=timeframe_minutes, policy=policy)
    reasons = []
    if variant != "baseline":
        if frame["status"] != "READY":
            reasons.append("WARMUP")
        else:
            if frame["side"] != side:
                reasons.append("NO_MATCHING_RANGE_BREAKOUT")
            if variant in ("range", "combined") and not frame["range_pass"]:
                reasons.append("RANGE_STRUCTURE_NOT_CONFIRMED")
            if variant in ("volume", "combined") and not frame["volume_pass"]:
                reasons.append("VOLUME_CONFIRMATION_MISSING")
    return {
        "allow": not reasons,
        "variant": variant,
        "reasons": reasons,
        "frame": frame,
    }
