"""Causal post-exit entry evidence; never an execution permission.

The caller supplies an exact, closed, ascending bar window and enforces source
freshness. A bounded POST_EXIT_ROLLING_BASE is a new, explicitly labelled price
structure, not a fabricated exit or an elapsed-time unlock. Its initial bars
are consumed as a baseline; only later bars can establish an opportunity.
"""

from dataclasses import asdict, dataclass
from decimal import localcontext

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar, decimal
from v2_core.reentry import (
    _FAILED_EXITS,
    _PROFIT_EXITS,
    _STAGNANT_EXITS,
    _UNATTRIBUTED_EXITS,
    ReentryPolicy,
)
from v2_core.reentry_checkpoint import advance, assess, seal, seed

VERSION = "trade-cycle-structure-v2"
LEGACY_VERSION = "trade-cycle-structure-v1"
MAX_BARS = 1440
MAX_TIME = 2**53 - 1


def opportunity_token(result):
    """Version-bound identity; callers must still validate scope and evidence."""
    return digest(
        canonical(
            {
                "version": result["version"],
                "previous_episode": result["previous_episode"],
                "previous_side": result["previous_side"],
                "side": result["side"],
                "policy_digest": result["policy_digest"],
                "anchor_basis": result["anchor_basis"],
                "anchor_start_ms": result["anchor_start_ms"],
                "break_closed_ms": result["break_closed_ms"],
                "trigger_closed_ms": result["trigger_closed_ms"],
                "path": result["path"],
            }
        )
    )


@dataclass(frozen=True)
class CyclePolicy:
    confirmation_bars: int = 2
    # Zero disables a mechanical lifespan. Unconsumed confirmed structure
    # remains valid until invalidated or chased; an explicit experiment can
    # still impose a finite bar-age limit.
    max_trigger_age_bars: int = 0

    def __post_init__(self):
        for key, low, high in (
            ("confirmation_bars", 2, 10),
            ("max_trigger_age_bars", 0, 30),
        ):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("TRADE_CYCLE_POLICY_INVALID")


def _time(value):
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise ValueError("TRADE_CYCLE_CLOCK_INVALID")
    return value


def _previous(previous, as_of):
    if not isinstance(previous, dict):
        raise ValueError("TRADE_CYCLE_PREVIOUS_INVALID")  # noqa: TRY004 - stable evidence boundary
    try:
        episode, side, reason = (
            previous["previous_episode"],
            previous["side"],
            previous["exit_reason"],
        )
        if (
            not isinstance(episode, str)
            or not 1 <= len(episode) <= 128
            or any(ord(c) < 32 for c in episode)
            or not isinstance(side, str)
            or side not in {"BUY", "SELL"}
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 128
            or any(ord(c) < 32 for c in reason)
        ):
            raise ValueError("TRADE_CYCLE_PREVIOUS_INVALID")
        exited = _time(previous["exit_known_ms"])
        if exited <= 0 or exited > as_of:
            raise ValueError("TRADE_CYCLE_EXIT_CLOCK_INVALID")
        price = decimal(previous["exit_price"])
        if price <= 0:
            raise ValueError("TRADE_CYCLE_EXIT_PRICE_INVALID")
        net = previous.get("exit_net_pnl")
        net = None if net is None else decimal(net)
    except (KeyError, TypeError) as exc:
        raise ValueError("TRADE_CYCLE_PREVIOUS_INVALID") from exc
    return {
        "previous_episode": episode,
        "side": side,
        "exit_reason": reason,
        "exit_known_ms": exited,
        "exit_price": str(price),
        "exit_net_pnl": None if net is None else str(net),
    }


def _validate_bars(bars, width, cutoff):
    if not isinstance(bars, (tuple, list)) or len(bars) > MAX_BARS:
        raise ValueError("TRADE_CYCLE_HISTORY_BOUND")
    for index, bar in enumerate(bars):
        if not isinstance(bar, Bar):
            raise ValueError("TRADE_CYCLE_BAR_INVALID")  # noqa: TRY004 - stable evidence boundary
        # Revalidate even an illegally mutated frozen dataclass at the boundary.
        if Bar(**bar.payload()) != bar:
            raise ValueError("TRADE_CYCLE_BAR_INVALID")
        if bar.t % width or bar.t + width > cutoff:
            raise ValueError("TRADE_CYCLE_UNCLOSED_BAR")
        if index and bar.t != bars[index - 1].t + width:
            raise ValueError("TRADE_CYCLE_HISTORY_GAP")
    if bars and bars[-1].t + width != cutoff:
        raise ValueError("TRADE_CYCLE_HISTORY_INCOMPLETE")


def _anchors(bars, previous, width, needed):
    exited = previous["exit_known_ms"]
    if not bars:
        return None
    if exited <= bars[0].t:
        if len(bars) < needed:
            return None
        baseline = list(bars[:needed])
        end = baseline[-1].t + width
        return {
            "basis": "POST_EXIT_ROLLING_BASE",
            "baseline": baseline,
            "seed_bars": baseline,
            "post": list(bars[needed:]),
            "effective_anchor_ms": end,
            "effective_price": str(baseline[-1].c),
        }
    pre = [bar for bar in bars if bar.t + width <= exited]
    if len(pre) < needed:
        return None
    end = (exited + width - 1) // width * width
    return {
        "basis": "EXIT_ANCHOR",
        "baseline": pre,
        # seed validates continuity including a possible straddling candle, but
        # excludes that candle from both its pre-exit basis and post-exit proof.
        "seed_bars": [bar for bar in bars if bar.t < end],
        "post": [bar for bar in bars if bar.t >= end],
        "effective_anchor_ms": exited,
        "effective_price": previous["exit_price"],
    }


def _same_side(anchor, previous, signal, as_of, cutoff, policy, cycle_policy):
    effective = {
        **previous,
        "exit_known_ms": anchor["effective_anchor_ms"],
        "exit_price": anchor["effective_price"],
    }
    checkpoint = seed(
        effective,
        anchor["seed_bars"],
        policy=policy,
        allow_unattributed_exit=True,
    )
    # A profitable-looking exit reason is not proof of a profitable trade.
    direct_break = (
        previous["exit_reason"] in _PROFIT_EXITS
        and previous["exit_net_pnl"] is not None
        and decimal(previous["exit_net_pnl"]) > 0
    )
    if not direct_break:
        checkpoint = seal({**checkpoint, "first_break": False})
    checkpoint = advance(checkpoint, anchor["post"], through_ms=cutoff)
    result = assess(
        checkpoint,
        signal_observed_ms=signal,
        as_of_ms=as_of,
        bar_cutoff_ms=cutoff,
        # Freshness belongs to the provider's configured source-age policy.
        # Do not add the checkpoint research helper's unrelated fixed 90s gate.
        max_bar_age_ms=as_of - cutoff + 1,
    )
    result["direct_breakout_eligible"] = direct_break
    if (
        anchor["basis"] == "POST_EXIT_ROLLING_BASE"
        and result["reason"] == "SIGNAL_PREDATES_EXIT"
    ):
        result["reason"] = "SIGNAL_PREDATES_CONFIRMATION"
    result.update(
        break_closed_ms=None,
        confirmation_bars_required=cycle_policy.confirmation_bars,
        confirmation_bars_observed=0,
    )
    trigger = checkpoint["last_trigger"]
    if trigger is not None:
        broken, path, reset, volume, level, invalidation = trigger
        sign = 1 if previous["side"] == "BUY" else -1
        width = policy.timeframe_minutes * 60000
        confirmation = [bar for bar in anchor["post"] if bar.t + width >= broken][
            : cycle_policy.confirmation_bars
        ]
        count = 0
        for bar in confirmation:
            if sign * bar.c <= decimal(level):
                break
            count += 1
        result.update(
            path=path,
            break_closed_ms=broken,
            trigger_closed_ms=None,
            reset_closed_ms=reset,
            relative_volume=volume,
            structure_invalidation_price=str(sign * decimal(invalidation)),
            confirmation_bars_observed=count,
            confirmation_interrupted=count < len(confirmation),
        )
        if count < cycle_policy.confirmation_bars:
            result.update(
                status="WAIT",
                reason="SIGNAL_PREDATES_CONFIRMATION"
                if signal < broken
                else "STRUCTURE_CONFIRMATION_PENDING",
            )
        else:
            confirmed = confirmation[-1].t + width
            age = (cutoff - confirmed) // width
            result.update(trigger_closed_ms=confirmed, trigger_age_bars=age)
            if signal < confirmed:
                result.update(status="WAIT", reason="SIGNAL_PREDATES_CONFIRMATION")
            elif (
                result["status"] == "READY"
                and cycle_policy.max_trigger_age_bars > 0
                and age > cycle_policy.max_trigger_age_bars
            ):
                result.update(status="WAIT", reason="STRUCTURE_TRIGGER_STALE")
    # The helper's effective anchoring clock must never masquerade as the
    # actual episode's exit clock in the enclosing trade-cycle proof.
    return {
        key: value
        for key, value in result.items()
        if key
        not in {
            "version",
            "policy",
            "policy_digest",
            "previous_episode",
            "side",
            "exit_reason",
            "exit_known_ms",
            "exit_price",
            "exit_net_pnl",
            "input_digest",
            "input_digest_method",
            "opportunity_token",
        }
    }


def _reverse(anchor, side, signal, cutoff, policy, cycle_policy):
    width = policy.timeframe_minutes * 60000
    pre, post = anchor["baseline"], anchor["post"]
    sign = 1 if side == "BUY" else -1
    with localcontext() as ctx:
        ctx.prec = 80
        true_ranges = [
            max(bar.h - bar.l, abs(bar.h - pre[i - 1].c), abs(bar.l - pre[i - 1].c))
            for i, bar in enumerate(pre)
            if i
        ]
        atr = sum(true_ranges[-policy.atr_period :]) / policy.atr_period
        if atr <= 0:
            return {"status": "UNKNOWN", "reason": "ZERO_ANCHOR_VOLATILITY"}
        bare_boundary = max(
            bar.h if sign == 1 else -bar.l for bar in pre[-policy.anchor_bars :]
        )
        boundary = bare_boundary + atr * decimal(policy.breakout_buffer_atr)
        evidence = {
            "path": "REVERSAL_BREAK_CONFIRM",
            "anchor_atr": str(atr),
            "atr_method": "FROZEN_SMA_TRUE_RANGE",
            "anchor_boundary": str(sign * boundary),
            "structure_invalidation_price": str(sign * bare_boundary),
            "confirmation_bars_required": cycle_policy.confirmation_bars,
            "confirmation_bars_observed": 0,
            "trigger_closed_ms": None,
            "break_closed_ms": None,
            "relative_volume": None,
        }

        def verdict(status, reason, **facts):
            return {**evidence, "status": status, "reason": reason, **facts}

        if not post:
            return verdict("WAIT", "NO_POST_EXIT_CLOSED_BAR")
        history = list(pre[-max(policy.volume_period, 2) :])
        pending, confirmed = None, None
        invalidated, insufficient_volume = False, False
        for bar in post:
            close = sign * bar.c
            preceding_close = sign * history[-1].c
            volume_base = sum(b.v for b in history[-policy.volume_period :]) / len(
                history[-policy.volume_period :]
            )
            relative_volume = bar.v / volume_base if volume_base > 0 else None
            if confirmed is not None and close <= bare_boundary:
                confirmed, pending, invalidated = None, None, True
            if confirmed is None:
                if pending is not None:
                    if close > boundary:
                        pending["count"] += 1
                    else:
                        pending = None
                if pending is None and preceding_close <= boundary < close:
                    if relative_volume is not None and relative_volume >= decimal(
                        policy.min_relative_volume
                    ):
                        pending = {
                            "count": 1,
                            "break_closed_ms": bar.t + width,
                            "relative_volume": str(relative_volume),
                        }
                    else:
                        insufficient_volume = True
                if (
                    pending is not None
                    and pending["count"] >= cycle_policy.confirmation_bars
                ):
                    confirmed = {**pending, "trigger_closed_ms": bar.t + width}
            history = (history + [bar])[-max(policy.volume_period, 2) :]
        if confirmed is None:
            return verdict(
                "WAIT",
                "REVERSAL_CONFIRMATION_PENDING"
                if pending is not None
                else "REVERSAL_STRUCTURE_INVALIDATED"
                if invalidated
                else "REVERSAL_VOLUME_UNCONFIRMED"
                if insufficient_volume
                else "REVERSAL_NOT_CONFIRMED",
                confirmation_bars_observed=pending["count"] if pending else 0,
                break_closed_ms=pending["break_closed_ms"] if pending else None,
                relative_volume=pending["relative_volume"] if pending else None,
            )
        evidence.update(
            trigger_closed_ms=confirmed["trigger_closed_ms"],
            break_closed_ms=confirmed["break_closed_ms"],
            relative_volume=confirmed["relative_volume"],
            confirmation_bars_observed=confirmed["count"],
            trigger_age_bars=(cutoff - confirmed["trigger_closed_ms"]) // width,
        )
        if signal < confirmed["trigger_closed_ms"]:
            return verdict("WAIT", "SIGNAL_PREDATES_CONFIRMATION")
        if (
            cycle_policy.max_trigger_age_bars > 0
            and evidence["trigger_age_bars"] > cycle_policy.max_trigger_age_bars
        ):
            return verdict("WAIT", "STRUCTURE_TRIGGER_STALE")
        if sign * post[-1].c - boundary > atr * decimal(policy.max_chase_atr):
            return verdict("WAIT", "STRUCTURE_CHASE_TOO_FAR")
        return verdict("READY", "REVERSAL_STRUCTURE_CONFIRMED")


def assess_trade_cycle(
    *,
    bars,
    previous,
    side,
    signal_observed_ms,
    as_of_ms,
    bar_cutoff_ms,
    policy,
    cycle_policy,
    rule_version=VERSION,
):
    """Assess an opportunity using only facts available by ``as_of_ms``.

    Invalid contracts raise fixed ValueErrors. Honest incomplete history returns
    UNKNOWN; a valid but unconfirmed structure returns WAIT. Neither may be
    represented as READY. Caller-owned scopes/locks still control admission.
    """
    if not isinstance(rule_version, str) or rule_version not in {
        LEGACY_VERSION,
        VERSION,
    }:
        raise ValueError("TRADE_CYCLE_VERSION_UNSUPPORTED")
    if not isinstance(policy, ReentryPolicy) or not isinstance(
        cycle_policy, CyclePolicy
    ):
        raise ValueError("TRADE_CYCLE_POLICY_REQUIRED")  # noqa: TRY004 - stable evidence boundary
    # Frozen dataclasses are also checked against illicit mutation.
    if (
        CyclePolicy(**asdict(cycle_policy)) != cycle_policy
        or ReentryPolicy(**asdict(policy)) != policy
    ):
        raise ValueError("TRADE_CYCLE_POLICY_INVALID")
    if not isinstance(side, str) or side not in {"BUY", "SELL"}:
        raise ValueError("TRADE_CYCLE_SIDE_INVALID")
    signal, as_of, cutoff = (
        _time(value) for value in (signal_observed_ms, as_of_ms, bar_cutoff_ms)
    )
    width = policy.timeframe_minutes * 60000
    if signal > as_of or cutoff > as_of or cutoff % width:
        raise ValueError("TRADE_CYCLE_CLOCK_INVALID")
    prior = _previous(previous, as_of)
    _validate_bars(bars, width, cutoff)
    result = {
        "version": rule_version,
        "previous_episode": prior["previous_episode"],
        "previous_side": prior["side"],
        "side": side,
        "transition": "SAME_SIDE_REENTRY"
        if prior["side"] == side
        else "OPPOSITE_SIDE_REVERSAL",
        "exit_reason": prior["exit_reason"],
        "exit_known_ms": prior["exit_known_ms"],
        "exit_price": prior["exit_price"],
        "exit_net_pnl": prior["exit_net_pnl"],
        "signal_observed_ms": signal,
        "as_of_ms": as_of,
        "bar_cutoff_ms": cutoff,
        "policy": asdict(policy),
        "cycle_policy": asdict(cycle_policy),
        "policy_digest": digest(
            canonical({"structure": asdict(policy), "cycle": asdict(cycle_policy)})
        ),
        "input_digest": digest(
            canonical({"previous": prior, "bars": [b.payload() for b in bars]})
        ),
        "anchor_basis": None,
        "path": None,
        "trigger_closed_ms": None,
        "structure_invalidation_price": None,
        "opportunity_token": None,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
    }

    def verdict(status, reason):
        return {**result, "status": status, "reason": reason}

    if signal <= prior["exit_known_ms"]:
        return verdict("WAIT", "SIGNAL_PREDATES_EXIT")
    supported_exits = _PROFIT_EXITS | _FAILED_EXITS | _STAGNANT_EXITS
    if rule_version == VERSION:
        supported_exits = supported_exits | _UNATTRIBUTED_EXITS
    if prior["exit_reason"] not in supported_exits:
        return verdict("UNKNOWN", "EXIT_REASON_UNSUPPORTED")
    if prior["exit_reason"] in _UNATTRIBUTED_EXITS:
        result["unattributed_exit_treatment"] = "POST_EXIT_STRUCTURE_REQUIRED"
    needed = max(policy.anchor_bars, policy.atr_period + 1, policy.volume_period)
    anchor = _anchors(bars, prior, width, needed)
    if anchor is None:
        return verdict("UNKNOWN", "ANCHOR_HISTORY_INCOMPLETE")
    result.update(
        anchor_basis=anchor["basis"],
        anchor_start_ms=anchor["baseline"][0].t,
        anchor_end_ms=anchor["baseline"][-1].t + width,
        effective_anchor_ms=anchor["effective_anchor_ms"],
        baseline_bars=len(anchor["baseline"]),
        post_anchor_bars=len(anchor["post"]),
    )
    if not anchor["post"]:
        return verdict("WAIT", "NO_POST_EXIT_CLOSED_BAR")
    if prior["side"] == side:
        try:
            assessment = _same_side(
                anchor, prior, signal, as_of, cutoff, policy, cycle_policy
            )
        except ValueError as exc:
            if str(exc) == "ZERO_ANCHOR_VOLATILITY":
                return verdict("UNKNOWN", "ZERO_ANCHOR_VOLATILITY")
            raise
    else:
        assessment = _reverse(anchor, side, signal, cutoff, policy, cycle_policy)
    result.update(assessment)
    if result["status"] == "READY":
        result["opportunity_token"] = opportunity_token(result)
    return result
