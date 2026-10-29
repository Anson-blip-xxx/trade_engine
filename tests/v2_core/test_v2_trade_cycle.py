from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from v2_core.quant_indicators import Bar
from v2_core.reentry import ReentryPolicy
from v2_core.trade_cycle import CyclePolicy, assess_trade_cycle

MINUTE = 60000
POLICY = ReentryPolicy(anchor_bars=3, atr_period=2, volume_period=3)


def bar(index, *, o="100", h="101", l="99", c="100", v="10", width=MINUTE):
    return Bar(index * width, o, h, l, c, v)


def previous(**changes):
    return {
        "previous_episode": "settled-episode-one",
        "side": "BUY",
        "exit_reason": "PEAK_DRAWDOWN",
        "exit_known_ms": 5 * MINUTE,
        "exit_price": "100",
        "exit_net_pnl": "2",
        **changes,
    }


def base():
    return [bar(i) for i in range(5)]


def breakout():
    return base() + [
        bar(5, h="103", c="102", v="20"),
        bar(6, o="102", h="102.5", l="101.8", c="102"),
    ]


def reversal():
    return base() + [
        bar(5, h="100", l="97.5", c="98", v="20"),
        bar(6, o="98", h="98.5", l="97", c="97.5", v="20"),
    ]


def evaluate(bars=None, *, side="BUY", prior=None, policy=POLICY, **changes):
    bars = breakout() if bars is None else bars
    cutoff = (
        bars[-1].t + getattr(policy, "timeframe_minutes", 1) * MINUTE
        if bars and isinstance(bars[-1], Bar)
        else 6 * MINUTE
    )
    values = {
        "bars": bars,
        "previous": previous() if prior is None else prior,
        "side": side,
        "signal_observed_ms": cutoff,
        "as_of_ms": cutoff + 1000,
        "bar_cutoff_ms": cutoff,
        "policy": policy,
        "cycle_policy": CyclePolicy(),
        **changes,
    }
    return assess_trade_cycle(**values)


def mirror(bars):
    return [Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars]


@pytest.mark.parametrize("value", [True, False, 1, 0, -1, 11, "2", 2.0, None])
def test_confirmation_policy_strict_bounds(value):
    with pytest.raises(ValueError, match="TRADE_CYCLE_POLICY_INVALID"):
        CyclePolicy(confirmation_bars=value)


@pytest.mark.parametrize("value", [True, False, -1, 31, "3", 3.0, None])
def test_trigger_age_policy_strict_bounds(value):
    with pytest.raises(ValueError, match="TRADE_CYCLE_POLICY_INVALID"):
        CyclePolicy(max_trigger_age_bars=value)


def test_same_direction_profitable_exit_can_take_genuine_new_break():
    result = evaluate()
    assert result["status"] == "READY"
    assert result["path"] == "NEW_BREAKOUT"
    assert result["break_closed_ms"] == 6 * MINUTE
    assert result["trigger_closed_ms"] == 7 * MINUTE
    assert result["anchor_basis"] == "EXIT_ANCHOR"
    assert result["previous_side"] == result["side"] == "BUY"
    assert result["direct_breakout_eligible"] is True
    assert result["execution_authorized"] is False
    assert result["automatic_parameter_updates"] is False
    assert len(result["opportunity_token"]) == 64


@pytest.mark.parametrize("net", [None, "0", "-2"])
def test_profitable_reason_without_positive_actual_net_cannot_direct_break(net):
    result = evaluate(prior=previous(exit_net_pnl=net))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_NOT_REARMED"
    assert result["direct_breakout_eligible"] is False
    assert result["opportunity_token"] is None


def test_omitted_net_is_unknown_not_a_profitable_exit():
    prior = previous()
    prior.pop("exit_net_pnl")
    assert evaluate(prior=prior)["status"] == "WAIT"


@pytest.mark.parametrize(
    "reason", ["EMERGENCY_LOSS", "TIME_STOP", "LOW_YIELD_STAGNATION"]
)
def test_positive_net_does_not_turn_failed_exit_reason_into_profit_exit(reason):
    result = evaluate(prior=previous(exit_reason=reason))
    assert result["status"] == "WAIT"
    assert result["direct_breakout_eligible"] is False


def test_loss_exit_can_reenter_after_actual_reset_and_repair():
    rows = base() + [
        bar(5, h="100", l="98", c="98.5", v="10"),
        bar(6, o="98.5", h="101", l="98.4", c="100.5", v="20"),
        bar(7, o="100.5", h="101", l="100.3", c="100.6"),
    ]
    result = evaluate(rows, prior=previous(exit_net_pnl="-1"))
    assert result["status"] == "READY"
    assert result["path"] == "PULLBACK_RESTART"
    assert result["reset_closed_ms"] == 6 * MINUTE
    assert result["break_closed_ms"] == 7 * MINUTE
    assert result["trigger_closed_ms"] == 8 * MINUTE


def test_flat_bars_or_elapsed_time_do_not_unlock_same_side():
    result = evaluate(base() + [bar(i) for i in range(5, 30)])
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_NOT_REARMED"


def test_same_direction_old_trigger_expires_without_minting_another():
    rows = breakout()
    for i in range(7, 11):
        rows.append(bar(i, o="102", h="102.5", l="101.8", c="102", v="10"))
    result = evaluate(rows, cycle_policy=CyclePolicy(max_trigger_age_bars=3))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_TRIGGER_STALE"
    assert result["trigger_closed_ms"] == 7 * MINUTE
    assert result["trigger_age_bars"] == 4
    assert result["opportunity_token"] is None


def test_trigger_age_limit_is_inclusive():
    rows = breakout() + [
        bar(i, o="102", h="102.5", l="101.8", c="102") for i in range(7, 10)
    ]
    result = evaluate(rows, cycle_policy=CyclePolicy(max_trigger_age_bars=3))
    assert result["status"] == "READY"
    assert result["trigger_age_bars"] == 3


def test_same_side_confirmation_must_not_come_from_future_of_signal():
    result = evaluate(signal_observed_ms=7 * MINUTE - 1)
    assert result["status"] == "WAIT"
    assert result["reason"] == "SIGNAL_PREDATES_CONFIRMATION"


@pytest.mark.parametrize("offset", [-1, 0])
def test_signal_must_follow_actual_exit(offset):
    result = evaluate(signal_observed_ms=5 * MINUTE + offset)
    assert result["reason"] == "SIGNAL_PREDATES_EXIT"


def test_reversal_requires_break_and_an_additional_closed_confirmation():
    result = evaluate(reversal()[:-1], side="SELL")
    assert result["status"] == "WAIT"
    assert result["reason"] == "REVERSAL_CONFIRMATION_PENDING"
    assert result["confirmation_bars_observed"] == 1
    result = evaluate(reversal(), side="SELL")
    assert result["status"] == "READY"
    assert result["path"] == "REVERSAL_BREAK_CONFIRM"
    assert result["previous_side"] == "BUY"
    assert result["side"] == "SELL"
    assert result["transition"] == "OPPOSITE_SIDE_REVERSAL"
    assert result["trigger_closed_ms"] == 7 * MINUTE
    assert result["break_closed_ms"] == 6 * MINUTE
    assert Decimal(result["anchor_boundary"]) == Decimal("98.7")
    assert Decimal(result["structure_invalidation_price"]) == 99


def test_reversal_ordinary_pullback_inside_original_range_is_not_confirmation():
    rows = base() + [bar(i, c="99.5", v="20") for i in range(5, 8)]
    result = evaluate(rows, side="SELL")
    assert result["status"] == "WAIT"
    assert result["reason"] == "REVERSAL_NOT_CONFIRMED"


def test_intrabar_low_without_broken_close_never_confirms_reversal():
    rows = base() + [bar(i, l="95", c="100", v="100") for i in range(5, 8)]
    assert evaluate(rows, side="SELL")["reason"] == "REVERSAL_NOT_CONFIRMED"


def test_reversal_confirmation_must_be_consecutive():
    rows = reversal()[:-1] + [bar(6, o="98", h="100.5", l="97.5", c="100", v="20")]
    assert evaluate(rows, side="SELL")["status"] == "WAIT"


def test_three_bar_confirmation_does_not_ready_after_two():
    result = evaluate(
        reversal(), side="SELL", cycle_policy=CyclePolicy(confirmation_bars=3)
    )
    assert result["reason"] == "REVERSAL_CONFIRMATION_PENDING"
    assert result["confirmation_bars_observed"] == 2


@pytest.mark.parametrize("volume", ["0", "10"])
def test_reversal_break_requires_volume_against_prior_bars(volume):
    rows = reversal()
    rows[5] = replace(rows[5], v=Decimal(volume))
    result = evaluate(rows, side="SELL")
    assert result["status"] == "WAIT"
    assert result["reason"] == "REVERSAL_VOLUME_UNCONFIRMED"


def test_zero_volume_baseline_is_unknown_volume_not_zero_threshold_pass():
    rows = [replace(b, v=Decimal(0)) for b in base()] + reversal()[5:]
    assert evaluate(rows, side="SELL")["reason"] == "REVERSAL_VOLUME_UNCONFIRMED"


def test_confirmed_reversal_loses_validity_when_price_recovers_prior_boundary():
    rows = reversal() + [bar(7, o="97.5", h="100", l="97", c="99", v="20")]
    result = evaluate(rows, side="SELL")
    assert result["status"] == "WAIT"
    assert result["reason"] == "REVERSAL_STRUCTURE_INVALIDATED"


def test_reversal_does_not_refresh_trigger_on_uninterrupted_move():
    rows = reversal() + [
        bar(i, o="97.5", h="98", l="97", c="97.5") for i in range(7, 11)
    ]
    result = evaluate(
        rows, side="SELL", cycle_policy=CyclePolicy(max_trigger_age_bars=3)
    )
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_TRIGGER_STALE"
    assert result["trigger_closed_ms"] == 7 * MINUTE


def test_reversal_rejects_current_chase_distance():
    rows = reversal() + [bar(7, o="97.5", h="98", l="90", c="91", v="30")]
    assert evaluate(rows, side="SELL")["reason"] == "STRUCTURE_CHASE_TOO_FAR"


def test_reversal_signal_must_observe_full_confirmation():
    result = evaluate(reversal(), side="SELL", signal_observed_ms=7 * MINUTE - 1)
    assert result["reason"] == "SIGNAL_PREDATES_CONFIRMATION"


@pytest.mark.parametrize("kind", ["same", "reverse"])
def test_buy_sell_price_reflection_symmetry(kind):
    rows = breakout() if kind == "same" else reversal()
    original_side = "BUY" if kind == "same" else "SELL"
    original = evaluate(rows, side=original_side)
    reflected = evaluate(
        mirror(rows),
        side="SELL" if original_side == "BUY" else "BUY",
        prior=previous(side="SELL"),
    )
    for key in ("status", "reason", "path", "trigger_closed_ms"):
        assert original[key] == reflected[key]
    assert (
        Decimal(original["structure_invalidation_price"])
        + Decimal(reflected["structure_invalidation_price"])
        == 200
    )


@pytest.mark.parametrize("scale", ["0.000001", "1000000"])
@pytest.mark.parametrize("kind", ["same", "reverse"])
def test_price_and_volume_unit_scaling_preserves_verdict(kind, scale):
    factor = Decimal(scale)
    rows = breakout() if kind == "same" else reversal()
    side = "BUY" if kind == "same" else "SELL"
    original = evaluate(rows, side=side)
    scaled = [
        Bar(b.t, b.o * factor, b.h * factor, b.l * factor, b.c * factor, b.v * 17)
        for b in rows
    ]
    changed = evaluate(scaled, side=side, prior=previous(exit_price=str(100 * factor)))
    for key in ("status", "reason", "path", "trigger_closed_ms", "relative_volume"):
        assert original[key] == changed[key]
    assert Decimal(changed["anchor_atr"]) == Decimal(original["anchor_atr"]) * factor


def test_exit_straddling_bar_is_not_post_exit_breakout():
    rows = base() + [
        bar(5, h="105", c="104", v="100"),
        bar(6, o="100", h="101", l="99", c="100"),
    ]
    result = evaluate(rows, prior=previous(exit_known_ms=5 * MINUTE + 1))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_NOT_REARMED"


def test_exit_straddling_bar_is_not_reversal_confirmation():
    rows = base() + [
        bar(5, h="100", l="95", c="96", v="100"),
        bar(6, o="100", h="101", l="99", c="100"),
    ]
    result = evaluate(rows, side="SELL", prior=previous(exit_known_ms=5 * MINUTE + 1))
    assert result["reason"] == "REVERSAL_NOT_CONFIRMED"


def test_long_gap_explicit_rolling_baseline_is_not_elapsed_time_unlock():
    rows = [bar(i) for i in range(1000, 1010)]
    result = evaluate(rows)
    assert result["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert result["exit_known_ms"] == 5 * MINUTE
    assert result["effective_anchor_ms"] == 1003 * MINUTE
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_NOT_REARMED"


def test_rolling_base_only_consumes_initial_bars_then_can_confirm_new_break():
    rows = [bar(i) for i in range(1000, 1003)] + [
        bar(1003, h="103", c="102", v="20"),
        bar(1004, o="102", h="102.5", l="101.8", c="102"),
    ]
    result = evaluate(rows)
    assert result["status"] == "READY"
    assert result["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert result["baseline_bars"] == 3
    assert result["post_anchor_bars"] == 2
    assert result["exit_known_ms"] == 5 * MINUTE
    assert result["exit_price"] == "100"
    assert result["break_closed_ms"] == 1004 * MINUTE
    assert result["trigger_closed_ms"] == 1005 * MINUTE


def test_rolling_base_reversal_also_requires_later_confirmation():
    rows = [replace(b, t=b.t + 1000 * MINUTE) for b in reversal()[2:]]
    result = evaluate(rows, side="SELL")
    assert result["status"] == "READY"
    assert result["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert result["previous_side"] == "BUY"
    assert result["trigger_closed_ms"] == 1007 * MINUTE


def test_only_baseline_bars_cannot_be_used_as_breakout_proof():
    rows = [bar(i) for i in range(1000, 1003)]
    result = evaluate(rows)
    assert result["status"] == "WAIT"
    assert result["reason"] == "NO_POST_EXIT_CLOSED_BAR"


def test_incomplete_recent_pre_exit_history_cannot_be_renamed_rolling_base():
    result = evaluate(breakout()[3:])
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "ANCHOR_HISTORY_INCOMPLETE"
    assert result["anchor_basis"] is None


def test_empty_history_is_honestly_unknown():
    assert evaluate([])["reason"] == "ANCHOR_HISTORY_INCOMPLETE"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_zero_atr_is_unknown(side):
    rows = [bar(i, h="100", l="100") for i in range(7)]
    result = evaluate(rows, side=side)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "ZERO_ANCHOR_VOLATILITY"


def test_unknown_exit_reason_is_not_fabricated_profit():
    result = evaluate(prior=previous(exit_reason="UNEXPLAINED_CLOSE"))
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "EXIT_REASON_UNSUPPORTED"


@pytest.mark.parametrize("field", ["signal_observed_ms", "as_of_ms", "bar_cutoff_ms"])
@pytest.mark.parametrize("value", [True, -1, "60000", 1.5, 2**53])
def test_invalid_clock_contracts(field, value):
    with pytest.raises(ValueError):
        evaluate(**{field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"signal_observed_ms": 8 * MINUTE},
        {"bar_cutoff_ms": 8 * MINUTE},
        {"bar_cutoff_ms": 7 * MINUTE - 1},
    ],
)
def test_future_and_unaligned_clocks_rejected(change):
    with pytest.raises(ValueError):
        evaluate(**change)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b[:2] + b[3:],
        lambda b: b[:2] + [b[1]] + b[2:],
        lambda b: list(reversed(b)),
        lambda b: b + [bar(7)],
        lambda b: b[:-1],
    ],
)
def test_missing_duplicate_reversed_future_or_incomplete_bars_rejected(mutate):
    with pytest.raises(ValueError):
        evaluate(
            mutate(breakout()),
            bar_cutoff_ms=7 * MINUTE,
            as_of_ms=7 * MINUTE + 1000,
            signal_observed_ms=7 * MINUTE,
        )


@pytest.mark.parametrize("field", ["exit_price", "exit_net_pnl"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", True, 1.5])
def test_bad_previous_numbers_rejected(field, value):
    with pytest.raises(ValueError):
        evaluate(prior=previous(**{field: value}))


def test_future_exit_rejected():
    with pytest.raises(ValueError):
        evaluate(prior=previous(exit_known_ms=8 * MINUTE))


def test_mutated_frozen_bar_is_revalidated():
    rows = breakout()
    object.__setattr__(rows[0], "c", Decimal("NaN"))
    with pytest.raises(ValueError):
        evaluate(rows)


def test_bad_policy_and_wrong_bar_types_rejected():
    with pytest.raises(ValueError):
        evaluate(policy={})
    with pytest.raises(ValueError):
        evaluate(cycle_policy={})
    with pytest.raises(ValueError):
        evaluate([{"t": 0}], bar_cutoff_ms=MINUTE, as_of_ms=6 * MINUTE + 1000)


def test_pure_inputs_unchanged_and_deterministic_token():
    rows, prior = breakout(), previous()
    saved_rows, saved_prior = deepcopy(rows), deepcopy(prior)
    first, second = evaluate(rows, prior=prior), evaluate(tuple(rows), prior=prior)
    assert first == second
    assert rows == saved_rows and prior == saved_prior


def test_different_previous_episode_or_policy_cannot_share_opportunity_identity():
    first = evaluate()
    episode = evaluate(prior=previous(previous_episode="other-settled-episode"))
    policy = evaluate(cycle_policy=CyclePolicy(max_trigger_age_bars=4))
    assert len({r["opportunity_token"] for r in (first, episode, policy)}) == 3


def test_reversal_new_signal_on_same_confirmed_structure_does_not_mint_new_token():
    first = evaluate(reversal(), side="SELL")
    later = evaluate(reversal(), side="SELL", signal_observed_ms=7 * MINUTE + 500)
    assert first["opportunity_token"] == later["opportunity_token"]


def test_five_minute_bars_use_bar_counts_not_one_minute_clocks():
    policy = replace(POLICY, timeframe_minutes=5)
    rows = [replace(b, t=b.t * 5) for b in reversal()]
    result = evaluate(
        rows, side="SELL", policy=policy, prior=previous(exit_known_ms=25 * MINUTE)
    )
    assert result["status"] == "READY"
    assert result["trigger_closed_ms"] == 35 * MINUTE


@pytest.mark.parametrize("side", [None, True, [], {}, "LONG", "buy"])
def test_invalid_direction_is_a_fixed_contract_error(side):
    with pytest.raises(ValueError, match="TRADE_CYCLE_SIDE_INVALID"):
        evaluate(side=side)


def test_profit_and_reversal_reason_both_required_for_direct_break():
    result = evaluate(prior=previous(exit_reason="ONE_HOUR_REVERSAL"))
    assert result["status"] == "READY"
    assert result["path"] == "NEW_BREAKOUT"


def test_ready_same_side_signal_updates_do_not_claim_distinct_opportunity():
    first = evaluate()
    rows = breakout() + [bar(7, o="102", h="102.5", l="101.8", c="102")]
    later = evaluate(rows)
    assert first["opportunity_token"] == later["opportunity_token"]
    assert first["input_digest"] != later["input_digest"]


def test_reversal_new_break_after_invalidation_has_new_trigger_and_identity():
    first = evaluate(reversal(), side="SELL")
    rows = reversal() + [
        bar(7, o="97.5", h="100.5", l="97", c="100", v="10"),
        bar(8, h="100", l="97.5", c="98", v="100"),
        bar(9, o="98", h="98.5", l="97", c="97.5", v="20"),
    ]
    later = evaluate(rows, side="SELL")
    assert later["status"] == "READY"
    assert later["trigger_closed_ms"] == 10 * MINUTE
    assert later["opportunity_token"] != first["opportunity_token"]


def test_rolling_basis_does_not_presume_profit_if_net_is_missing():
    rows = [bar(i) for i in range(1000, 1003)] + [bar(1003, h="103", c="102", v="20")]
    result = evaluate(rows, prior=previous(exit_net_pnl=None))
    assert result["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_NOT_REARMED"


def test_incomplete_rolling_base_remains_unknown():
    rows = [bar(i) for i in range(1000, 1002)]
    assert evaluate(rows)["reason"] == "ANCHOR_HISTORY_INCOMPLETE"


def test_rolling_baseline_is_not_allowed_to_confirm_an_older_signal():
    rows = [bar(i) for i in range(1000, 1003)] + [bar(1003, h="103", c="102", v="20")]
    result = evaluate(rows, signal_observed_ms=1002 * MINUTE)
    assert result["exit_known_ms"] == 5 * MINUTE
    assert result["status"] == "WAIT"
    assert result["reason"] == "SIGNAL_PREDATES_CONFIRMATION"


def test_flat_bar_extension_is_never_a_new_reversal_trigger():
    first = evaluate(reversal(), side="SELL")
    rows = reversal() + [bar(7, o="97.5", h="98", l="97", c="97.5")]
    second = evaluate(rows, side="SELL")
    assert first["opportunity_token"] == second["opportunity_token"]
    assert second["trigger_closed_ms"] == 7 * MINUTE


def test_boundary_and_atr_are_frozen_before_the_reversal_break():
    first = evaluate(reversal(), side="SELL")
    rows = reversal() + [bar(7, o="97.5", h="150", l="90", c="97.5", v="1000")]
    second = evaluate(rows, side="SELL")
    for key in ("anchor_boundary", "anchor_atr", "structure_invalidation_price"):
        assert first[key] == second[key]


def test_default_policy_does_not_impose_a_mechanical_opportunity_ttl():
    assert CyclePolicy().max_trigger_age_bars == 0
    assert CyclePolicy(max_trigger_age_bars=0) == CyclePolicy()


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_single_same_direction_breakout_is_not_confirmation(side):
    rows = breakout()[:-1]
    if side == "SELL":
        rows = mirror(rows)
    result = evaluate(rows, side=side, prior=previous(side=side))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["break_closed_ms"] == 6 * MINUTE
    assert result["trigger_closed_ms"] is None
    assert result["confirmation_bars_observed"] == 1
    assert result["opportunity_token"] is None


def test_single_same_direction_pullback_restart_is_not_confirmation():
    rows = base() + [
        bar(5, h="100", l="98", c="98.5"),
        bar(6, o="98.5", h="101", l="98.4", c="100.5", v="20"),
    ]
    result = evaluate(rows, prior=previous(exit_net_pnl="-1"))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["path"] == "PULLBACK_RESTART"
    assert result["break_closed_ms"] == 7 * MINUTE
    assert result["trigger_closed_ms"] is None


def test_same_direction_confirmation_must_hold_beyond_trigger_level():
    rows = breakout()[:-1] + [bar(6, o="102", h="102.5", l="101.1", c="101.2")]
    result = evaluate(rows)
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["confirmation_interrupted"] is True
    assert result["confirmation_bars_observed"] == 1


def test_interrupted_same_side_confirmation_cannot_be_completed_retroactively():
    rows = breakout()[:-1] + [
        bar(6, o="102", h="102.5", l="101.1", c="101.2"),
        bar(7, o="101.2", h="102.5", l="101.1", c="102"),
    ]
    result = evaluate(rows)
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["trigger_closed_ms"] is None


def test_same_side_higher_confirmation_count_requires_corresponding_closes():
    result = evaluate(cycle_policy=CyclePolicy(confirmation_bars=3))
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["confirmation_bars_observed"] == 2
    rows = breakout() + [bar(7, o="102", h="102.5", l="101.8", c="102")]
    confirmed = evaluate(rows, cycle_policy=CyclePolicy(confirmation_bars=3))
    assert confirmed["status"] == "READY"
    assert confirmed["break_closed_ms"] == 6 * MINUTE
    assert confirmed["trigger_closed_ms"] == 8 * MINUTE


def test_unconsumed_same_side_confirmed_structure_survives_flat_bar_age():
    first = evaluate()
    rows = breakout() + [
        bar(i, o="102", h="102.5", l="101.8", c="102") for i in range(7, 40)
    ]
    later = evaluate(rows)
    assert later["status"] == "READY"
    assert later["trigger_age_bars"] == 33
    assert later["break_closed_ms"] == 6 * MINUTE
    assert later["trigger_closed_ms"] == 7 * MINUTE
    assert later["opportunity_token"] == first["opportunity_token"]


def test_unconsumed_reversal_confirmed_structure_survives_flat_bar_age():
    first = evaluate(reversal(), side="SELL")
    rows = reversal() + [
        bar(i, o="97.5", h="98", l="97", c="97.5") for i in range(7, 40)
    ]
    later = evaluate(rows, side="SELL")
    assert later["status"] == "READY"
    assert later["trigger_age_bars"] == 33
    assert later["opportunity_token"] == first["opportunity_token"]


def test_disabled_ttl_does_not_bypass_same_side_structural_invalidation():
    rows = breakout() + [bar(7, o="102", h="103", l="99", c="100", v="100")]
    assert evaluate(rows)["status"] == "WAIT"


def test_disabled_ttl_does_not_bypass_same_side_chasing_limit():
    rows = breakout() + [bar(7, o="102", h="111", l="101.5", c="110", v="100")]
    result = evaluate(rows)
    assert result["status"] == "WAIT"
    assert result["reason"] == "STRUCTURE_CHASE_TOO_FAR"


def test_post_exit_rolling_breakout_requires_second_closed_confirmation():
    rows = [bar(i) for i in range(1000, 1003)] + [bar(1003, h="103", c="102", v="20")]
    result = evaluate(rows)
    assert result["anchor_basis"] == "POST_EXIT_ROLLING_BASE"
    assert result["reason"] == "STRUCTURE_CONFIRMATION_PENDING"
    assert result["trigger_closed_ms"] is None
