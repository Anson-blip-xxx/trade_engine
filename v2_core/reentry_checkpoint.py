"""Bounded, restartable form of the structural research rule.

Keep the ORIGINAL exit anchor and every subsequent transition. A time gap is
never an unlock or a new anchor. No I/O or order authority. Differential tests
against reentry.assess_reentry are the compatibility contract.
"""

from copy import deepcopy
from dataclasses import asdict
from decimal import localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal
from v2_core.reentry import (
    _FAILED_EXITS,
    _PROFIT_EXITS,
    _STAGNANT_EXITS,
    VERSION,
    ReentryPolicy,
)


def seal(state):
    state = {k: v for k, v in state.items() if k != "digest"}
    return {**state, "digest": digest(canonical(state))}


def verify(state):
    if (
        not isinstance(state, dict)
        or state.get("version") != "reentry-checkpoint-v1"
        or state.get("digest") != seal(state)["digest"]
        or state.get("execution_authorized") is not False
    ):
        raise ValueError("REENTRY_CHECKPOINT_INVALID")
    policy = ReentryPolicy(**state["identity"]["policy"])
    if (
        type(state["through_ms"]) is not int
        or state["through_ms"] < state["identity"]["exit_known_ms"]
        or state["through_ms"] % (policy.timeframe_minutes * 60000)
        or not 1 <= len(state["history"]) <= max(policy.volume_period, 2)
    ):
        raise ValueError("REENTRY_CHECKPOINT_INVALID")
    return policy


def identity(previous, policy):
    return {**previous, "policy": asdict(policy)}


def _bars(bars, *, start, end, width):
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end < start
        or end > 2**53 - 1
        or start % width
        or end % width
        or not isinstance(bars, (tuple, list))
        or len(bars) > 1440
        or len(bars) != (end - start) // width
        or any(
            not isinstance(b, Bar) or b.t != start + i * width
            for i, b in enumerate(bars)
        )
    ):
        raise ValueError("REENTRY_CHECKPOINT_HISTORY_GAP")


def seed(previous, bars, *, policy=None):
    policy = policy or ReentryPolicy()
    width = policy.timeframe_minutes * 60000
    exited = previous["exit_known_ms"]
    if (
        type(exited) is not int
        or not 0 < exited <= 2**53 - 1
        or previous["side"] not in {"BUY", "SELL"}
        or not isinstance(previous["previous_episode"], str)
        or not previous["previous_episode"]
    ):
        raise ValueError("REENTRY_CHECKPOINT_IDENTITY")
    if previous["exit_reason"] not in _PROFIT_EXITS | _FAILED_EXITS | _STAGNANT_EXITS:
        raise ValueError("EXIT_REASON_UNSUPPORTED")
    end = ((exited + width - 1) // width) * width
    _bars(bars, start=bars[0].t if bars else end, end=end, width=width)
    pre = [b for b in bars if b.t + width <= exited]
    if len(pre) < max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period):
        raise ValueError("ANCHOR_HISTORY_INCOMPLETE")
    with localcontext() as ctx:
        ctx.prec = 80
        tr = [
            max(b.h - b.l, abs(b.h - pre[i - 1].c), abs(b.l - pre[i - 1].c))
            for i, b in enumerate(pre)
            if i
        ]
        atr = sum(tr[-policy.atr_period :]) / policy.atr_period
        if atr <= 0:
            raise ValueError("ZERO_ANCHOR_VOLATILITY")
        sign = 1 if previous["side"] == "BUY" else -1
        px = decimal(previous["exit_price"])
        if px <= 0:
            raise ValueError("REENTRY_EXIT_PRICE")
        boundary = max(
            b.h if sign == 1 else -b.l for b in pre[-policy.anchor_bars :]
        ) + atr * decimal(policy.breakout_buffer_atr)
        return seal(
            {
                "version": "reentry-checkpoint-v1",
                "identity": identity(previous, policy),
                "execution_authorized": False,
                "through_ms": end,
                "post_count": 0,
                "atr": str(atr),
                "boundary": str(boundary),
                "peak": str(sign * px),
                "reset_at": None,
                "reset_floor": None,
                "last_trigger": None,
                "first_break": previous["exit_reason"] in _PROFIT_EXITS,
                "history": [b.payload() for b in pre[-max(policy.volume_period, 2) :]],
                "input_digest": digest(
                    canonical(
                        {
                            "identity": identity(previous, policy),
                            "seed": [b.payload() for b in bars],
                        }
                    )
                ),
            }
        )


def advance(checkpoint, bars, *, through_ms):
    policy = verify(checkpoint)
    width = policy.timeframe_minutes * 60000
    _bars(bars, start=checkpoint["through_ms"], end=through_ms, width=width)
    state = deepcopy(checkpoint)
    side = state["identity"]["side"]
    sign = 1 if side == "BUY" else -1
    with localcontext() as ctx:
        ctx.prec = 80
        atr, boundary, peak = (decimal(state[k]) for k in ("atr", "boundary", "peak"))
        reset_at, first_break = state["reset_at"], state["first_break"]
        reset_floor = (
            decimal(state["reset_floor"]) if state["reset_floor"] is not None else None
        )
        last = state["last_trigger"]
        if last is not None:
            last = (*last[:4], decimal(last[4]), decimal(last[5]))
        history = [Bar(**b) for b in state["history"]]
        close = lambda b: sign * b.c
        high = lambda b: b.h if sign == 1 else -b.l
        for b in bars:
            if last is not None and close(b) <= last[5]:
                last = None
            previous = history[-1]
            volume_base = (
                sum(x.v for x in history[-policy.volume_period :])
                / policy.volume_period
            )
            relative_volume = b.v / volume_base if volume_base > 0 else None
            volume_ok = relative_volume is not None and relative_volume >= decimal(
                policy.min_relative_volume
            )
            if reset_at is None and peak - close(b) >= atr * decimal(policy.reset_atr):
                reset_at, reset_floor = b.t + width, close(b)
            if reset_at is not None:
                reset_floor = min(reset_floor, close(b))
            resumed = (
                reset_at is not None
                and reset_at <= b.t
                and close(b) > high(previous) + atr * decimal(policy.resume_buffer_atr)
            )
            breakout = first_break and close(previous) <= boundary < close(b)
            expansion = state["identity"][
                "exit_reason"
            ] not in _STAGNANT_EXITS or b.h - b.l >= atr * decimal(
                policy.stagnation_expansion_atr
            )
            level = (
                high(previous) + atr * decimal(policy.resume_buffer_atr)
                if resumed
                else boundary
            )
            if (
                (resumed or breakout)
                and volume_ok
                and expansion
                and close(b) - level <= atr * decimal(policy.max_chase_atr)
            ):
                invalidation = (
                    reset_floor
                    if resumed
                    else boundary - atr * decimal(policy.breakout_buffer_atr)
                )
                last = (
                    b.t + width,
                    "PULLBACK_RESTART" if resumed else "NEW_BREAKOUT",
                    reset_at,
                    str(relative_volume),
                    level,
                    invalidation,
                )
                reset_at, reset_floor, first_break, peak = None, None, False, close(b)
            else:
                peak = max(peak, close(b))
            history = (history + [b])[-max(policy.volume_period, 2) :]
            state["input_digest"] = digest(
                canonical({"previous": state["input_digest"], "bar": b.payload()})
            )
        state.update(
            through_ms=through_ms,
            post_count=state["post_count"] + len(bars),
            peak=str(peak),
            reset_at=reset_at,
            reset_floor=str(reset_floor) if reset_floor is not None else None,
            first_break=first_break,
            last_trigger=[*last[:4], str(last[4]), str(last[5])]
            if last is not None
            else None,
            history=[b.payload() for b in history],
        )
    return seal(state)


def assess(
    checkpoint,
    *,
    signal_observed_ms,
    as_of_ms,
    bar_cutoff_ms=None,
    max_bar_age_ms=90000,
):
    policy = verify(checkpoint)
    if (
        any(
            type(t) is not int or not 0 <= t <= 2**53 - 1
            for t in (signal_observed_ms, as_of_ms)
        )
        or signal_observed_ms > as_of_ms
    ):
        raise ValueError("REENTRY_CLOCK")
    previous = checkpoint["identity"]
    width = policy.timeframe_minutes * 60000
    cutoff = as_of_ms // width * width if bar_cutoff_ms is None else bar_cutoff_ms
    if (
        type(cutoff) is not int
        or cutoff < 0
        or cutoff % width
        or cutoff > as_of_ms
        or type(max_bar_age_ms) is not int
        or max_bar_age_ms <= 0
    ):
        raise ValueError("REENTRY_BAR_CLOCK")
    side = previous["side"]
    sign = 1 if side == "BUY" else -1
    result = {
        "version": VERSION,
        "checkpoint_digest": checkpoint["digest"],
        "input_digest": checkpoint["input_digest"],
        "input_digest_method": "ORDERED_BAR_HASH_CHAIN_V1",
        **previous,
        "policy_digest": digest(canonical(asdict(policy))),
        "signal_observed_ms": signal_observed_ms,
        "as_of_ms": as_of_ms,
        "bar_cutoff_ms": cutoff,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "path": None,
        "trigger_closed_ms": None,
        "reset_closed_ms": None,
        "opportunity_token": None,
        "anchor_atr": checkpoint["atr"],
        "atr_method": "FROZEN_SMA_TRUE_RANGE",
    }

    def verdict(status, reason, **extra):
        return {**result, "status": status, "reason": reason, **extra}

    if as_of_ms - cutoff >= max_bar_age_ms:
        return verdict("UNKNOWN", "REENTRY_MARKET_STALE")
    if checkpoint["through_ms"] != cutoff:
        return verdict("UNKNOWN", "REENTRY_CHECKPOINT_CATCHING_UP")
    if signal_observed_ms <= previous["exit_known_ms"]:
        return verdict("WAIT", "SIGNAL_PREDATES_EXIT")
    if not checkpoint["post_count"]:
        return verdict("WAIT", "NO_POST_EXIT_CLOSED_BAR")
    last = checkpoint["last_trigger"]
    if last is None:
        return verdict(
            "WAIT", "STRUCTURE_NOT_REARMED", reset_closed_ms=checkpoint["reset_at"]
        )
    triggered, path, reset, volume, level, invalidation = last
    if signal_observed_ms < triggered:
        return verdict("WAIT", "SIGNAL_PREDATES_CONFIRMATION")
    with localcontext() as ctx:
        ctx.prec = 80
        result["anchor_boundary"] = str(sign * decimal(checkpoint["boundary"]))
        if sign * decimal(checkpoint["history"][-1]["c"]) - decimal(level) > decimal(
            checkpoint["atr"]
        ) * decimal(policy.max_chase_atr):
            return verdict("WAIT", "STRUCTURE_CHASE_TOO_FAR")
        token = digest(
            canonical(
                {
                    "version": VERSION,
                    "previous_episode": previous["previous_episode"],
                    "side": side,
                    "policy": result["policy_digest"],
                    "trigger_closed_ms": triggered,
                    "path": path,
                }
            )
        )
        return verdict(
            "READY",
            "NEW_STRUCTURE_CONFIRMED",
            path=path,
            trigger_closed_ms=triggered,
            opportunity_token=token,
            reset_closed_ms=reset,
            relative_volume=volume,
            structure_invalidation_price=str(sign * decimal(invalidation)),
        )
