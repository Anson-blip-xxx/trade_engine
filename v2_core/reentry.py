"""Causal structural re-entry research. A READY result is NOT an order permit.

No elapsed-time cooldown, future pivots, PnL labels, source voting or clock I/O.
All thresholds are versioned research parameters, not calibrated live defaults.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal

VERSION = "structural-reentry-v1"
_PROFIT_EXITS = {"PEAK_DRAWDOWN", "ONE_HOUR_REVERSAL"}
_FAILED_EXITS = {
    "EARLY_LOSS_MOMENTUM_WEAK",
    "EMERGENCY_LOSS",
    "VENUE_PROTECTION_CHILD",
    "TIME_STOP",
}
_STAGNANT_EXITS = {"LOW_YIELD_STAGNATION"}


@dataclass(frozen=True)
class ReentryPolicy:
    timeframe_minutes: int = 1
    anchor_bars: int = 20
    atr_period: int = 14
    volume_period: int = 20
    reset_atr: str = "0.5"
    breakout_buffer_atr: str = "0.15"
    resume_buffer_atr: str = "0.10"
    min_relative_volume: str = "1.2"
    max_chase_atr: str = "1.5"
    stagnation_expansion_atr: str = "1.2"

    def __post_init__(self):
        if type(self.timeframe_minutes) is not int or self.timeframe_minutes not in (
            1,
            5,
            15,
        ):
            raise ValueError("REENTRY_TIMEFRAME")
        for field in ("anchor_bars", "atr_period", "volume_period"):
            value = getattr(self, field)
            if type(value) is not int or not 2 <= value <= 200:
                raise ValueError("REENTRY_PERIOD")
        for field in (
            "reset_atr",
            "breakout_buffer_atr",
            "resume_buffer_atr",
            "min_relative_volume",
            "max_chase_atr",
            "stagnation_expansion_atr",
        ):
            if not Decimal("0.01") <= decimal(getattr(self, field)) <= Decimal(20):
                raise ValueError("REENTRY_THRESHOLD")
            object.__setattr__(
                self, field, format(decimal(getattr(self, field)).normalize(), "f")
            )
        if decimal(self.reset_atr) <= decimal(self.resume_buffer_atr):
            raise ValueError("REENTRY_HYSTERESIS_REQUIRED")


def assess_reentry(
    *,
    bars,
    side,
    previous_episode,
    exit_reason,
    exit_known_ms,
    signal_observed_ms,
    as_of_ms,
    exit_price=None,
    policy=None,
):
    policy = policy or ReentryPolicy()
    if not isinstance(policy, ReentryPolicy):
        raise TypeError("REENTRY_POLICY_REQUIRED")
    if (
        side not in {"BUY", "SELL"}
        or not isinstance(previous_episode, str)
        or not previous_episode
    ):
        raise ValueError("REENTRY_IDENTITY")
    if any(
        type(t) is not int or not 0 <= t <= 2**53 - 1
        for t in (exit_known_ms, signal_observed_ms, as_of_ms)
    ):
        raise ValueError("REENTRY_CLOCK")
    if exit_known_ms > as_of_ms or signal_observed_ms > as_of_ms:
        raise ValueError("REENTRY_FUTURE_EVENT")
    if (
        not isinstance(bars, (tuple, list))
        or len(bars) > 2500
        or any(not isinstance(b, Bar) for b in bars)
    ):
        raise ValueError("REENTRY_BARS_BOUND")
    width = policy.timeframe_minutes * 60000
    for i, bar in enumerate(bars):
        if bar.t % width or bar.t + width > as_of_ms:
            raise ValueError("REENTRY_UNCLOSED_BAR")
        if i and bar.t != bars[i - 1].t + width:
            raise ValueError("REENTRY_HISTORY_GAP")
    proof = {
        "version": VERSION,
        "policy": asdict(policy),
        "policy_digest": digest(canonical(asdict(policy))),
        "side": side,
        "previous_episode": previous_episode,
        "exit_reason": exit_reason,
        "exit_known_ms": exit_known_ms,
        "signal_observed_ms": signal_observed_ms,
        "as_of_ms": as_of_ms,
        "status": "UNKNOWN",
        "reason": None,
        "path": None,
        "trigger_closed_ms": None,
        "opportunity_token": None,
        "reset_closed_ms": None,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
    }

    def result(status, reason, **facts):
        return {**proof, "status": status, "reason": reason, **facts}

    if signal_observed_ms <= exit_known_ms:
        return result("WAIT", "SIGNAL_PREDATES_EXIT")
    if exit_reason not in _PROFIT_EXITS | _FAILED_EXITS | _STAGNANT_EXITS:
        return result("UNKNOWN", "EXIT_REASON_UNSUPPORTED")
    if exit_price is None:
        return result("UNKNOWN", "EXIT_PRICE_MISSING")
    exit_px = decimal(exit_price)
    if exit_px <= 0:
        raise ValueError("REENTRY_EXIT_PRICE")
    proof["exit_price"] = str(exit_px)
    if not bars or bars[-1].t + width != as_of_ms // width * width:
        return result("UNKNOWN", "HISTORY_STALE")
    pre = [b for b in bars if b.t + width <= exit_known_ms]
    post = [b for b in bars if b.t >= exit_known_ms]
    needed = max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period)
    if len(pre) < needed:
        return result("UNKNOWN", "ANCHOR_HISTORY_INCOMPLETE")
    # The bar straddling the exit is not post-exit repair evidence.
    if not post:
        return result("WAIT", "NO_POST_EXIT_CLOSED_BAR")
    with localcontext() as ctx:
        ctx.prec = 80
        tr = [
            max(b.h - b.l, abs(b.h - pre[i - 1].c), abs(b.l - pre[i - 1].c))
            for i, b in enumerate(pre)
            if i
        ]
        # Freeze the precisely labelled SMA-TR research volatility at the exit.
        # Do not reinterpret it as the legacy ATR or a Wilder/Pine ATR.
        atr = sum(tr[-policy.atr_period :]) / policy.atr_period
        if atr <= 0:
            return result("UNKNOWN", "ZERO_ANCHOR_VOLATILITY")
        sign = 1 if side == "BUY" else -1
        close = lambda b: sign * b.c
        high = lambda b: b.h if sign == 1 else -b.l
        boundary = max(high(b) for b in pre[-policy.anchor_bars :]) + atr * decimal(
            policy.breakout_buffer_atr
        )
        # Repair must occur AFTER exit, not merely remain below an old peak.
        # Close-watermarks avoid inferring intra-bar high/low order.
        peak = sign * exit_px
        reset_at = None
        reset_floor = None
        # An initial continuation breakout is allowed only after profit exits.
        # Failed/stagnant exits must first repair structure, not simply resume.
        first_break = exit_reason in _PROFIT_EXITS
        last_trigger = None
        history = list(pre)
        details = {
            "anchor_atr": str(atr),
            "atr_method": "FROZEN_SMA_TRUE_RANGE",
            "anchor_boundary": str(sign * boundary),
            "input_digest": digest(canonical({"bars": [b.payload() for b in bars]})),
        }
        for b in post:
            if last_trigger is not None and close(b) <= last_trigger[5]:
                last_trigger = None
            previous = history[-1]
            volume_base = (
                sum(x.v for x in history[-policy.volume_period :])
                / policy.volume_period
            )
            relative_volume = b.v / volume_base if volume_base > 0 else None
            volume_ok = relative_volume is not None and relative_volume >= decimal(
                policy.min_relative_volume
            )
            # Compare with an earlier peak, never with this bar's unknown OHLC order.
            if reset_at is None and peak - close(b) >= atr * decimal(policy.reset_atr):
                reset_at = b.t + width
                reset_floor = close(b)
            if reset_at is not None:
                reset_floor = min(reset_floor, close(b))
            reset_before = reset_at is not None and reset_at <= b.t
            resumed = reset_before and close(b) > high(previous) + atr * decimal(
                policy.resume_buffer_atr
            )
            breakout = first_break and close(previous) <= boundary < close(b)
            expansion = (
                exit_reason not in _STAGNANT_EXITS
                or b.h - b.l >= atr * decimal(policy.stagnation_expansion_atr)
            )
            trigger_level = (
                high(previous) + atr * decimal(policy.resume_buffer_atr)
                if resumed
                else boundary
            )
            not_chasing = close(b) - trigger_level <= atr * decimal(
                policy.max_chase_atr
            )
            if (resumed or breakout) and volume_ok and expansion and not_chasing:
                path = "PULLBACK_RESTART" if resumed else "NEW_BREAKOUT"
                invalidation = (
                    reset_floor
                    if resumed
                    else boundary - atr * decimal(policy.breakout_buffer_atr)
                )
                last_trigger = (
                    b.t + width,
                    path,
                    reset_at,
                    str(relative_volume),
                    trigger_level,
                    invalidation,
                )
                reset_at, first_break = None, False
                reset_floor = None
                # A subsequent structural opportunity requires another real reset.
                peak = close(b)
            else:
                peak = max(peak, close(b))
            history.append(b)
        if last_trigger is None:
            return result(
                "WAIT", "STRUCTURE_NOT_REARMED", reset_closed_ms=reset_at, **details
            )
        triggered, path, reset_used, volume, trigger_level, invalidation = last_trigger
        # Signals must observe the confirming bar. Old updates cannot inherit
        # a structure which formed after their observation timestamp.
        if signal_observed_ms < triggered:
            return result("WAIT", "SIGNAL_PREDATES_CONFIRMATION", **details)
        # A valid structure persists, not just for a fixed number of minutes.
        # No new opportunity token is minted by another signal or flat bar.
        if close(bars[-1]) - trigger_level > atr * decimal(policy.max_chase_atr):
            return result("WAIT", "STRUCTURE_CHASE_TOO_FAR", **details)
        token = digest(
            canonical(
                {
                    "version": VERSION,
                    "previous_episode": previous_episode,
                    "side": side,
                    "policy": proof["policy_digest"],
                    "trigger_closed_ms": triggered,
                    "path": path,
                }
            )
        )
        return result(
            "READY",
            "NEW_STRUCTURE_CONFIRMED",
            path=path,
            trigger_closed_ms=triggered,
            opportunity_token=token,
            reset_closed_ms=reset_used,
            relative_volume=volume,
            structure_invalidation_price=str(sign * invalidation),
            **details,
        )
