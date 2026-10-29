from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from v2_core.quant_indicators import Bar
from v2_core.reentry import ReentryPolicy, assess_reentry


def bar(t, o="100", h="101", l="99", c="100", v="10"):
    return Bar(t * 60000, o, h, l, c, v)


def history(*after):
    return tuple([bar(i) for i in range(25)] + list(after))


def assess(bars, **changes):
    args = {
        "bars": bars,
        "side": "BUY",
        "previous_episode": "previous",
        "exit_reason": "PEAK_DRAWDOWN",
        "exit_known_ms": 25 * 60000,
        "signal_observed_ms": (bars[-1].t + 60000),
        "as_of_ms": (bars[-1].t + 60000),
        "exit_price": "100",
    }
    args.update(changes)
    return assess_reentry(**args)


def test_new_breakout_not_elapsed_time_unlocks():
    rising = history(bar(25, h="102", c="101.5", v="20"))
    result = assess(rising)
    assert result["status"] == "READY" and result["path"] == "NEW_BREAKOUT"
    assert not result["execution_authorized"]
    flat = history(*(bar(i) for i in range(25, 100)))
    assert assess(flat)["status"] == "WAIT"


def test_repair_requires_later_bar_and_separate_threshold():
    first = bar(25, h="100", l="98", c="99", v="20")
    second = bar(26, o="99", h="101", l="99", c="100.5", v="20")
    assert assess(history(first))["status"] == "WAIT"
    result = assess(history(first, second))
    assert result["path"] == "PULLBACK_RESTART"
    assert result["reset_closed_ms"] == 26 * 60000


def test_failed_exit_cannot_immediately_reuse_trend():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    for reason in (
        "EMERGENCY_LOSS",
        "VENUE_PROTECTION_CHILD",
        "EARLY_LOSS_MOMENTUM_WEAK",
    ):
        assert assess(bars, exit_reason=reason)["status"] == "WAIT"
    repaired = history(
        bar(25, h="100", l="98", c="99"),
        bar(26, o="99", h="101", l="99", c="100.5", v="20"),
    )
    assert assess(repaired, exit_reason="EMERGENCY_LOSS")["status"] == "READY"


def test_stagnation_requires_expansion_in_addition_to_repair():
    bars = history(
        bar(25, h="100", l="98", c="99"),
        bar(26, o="99", h="101", l="99", c="100.5", v="20"),
    )
    assert assess(bars, exit_reason="LOW_YIELD_STAGNATION")["status"] == "WAIT"
    assert (
        assess(
            bars,
            exit_reason="LOW_YIELD_STAGNATION",
            policy=ReentryPolicy(stagnation_expansion_atr="0.5"),
        )["status"]
        == "READY"
    )


def test_no_same_bar_ohlc_order_assumption_and_no_repeated_revision_unlock():
    wick = history(bar(25, h="105", l="99.5", c="100", v="20"))
    assert assess(wick)["status"] == "WAIT"
    bars = history(bar(25, h="102", c="101.5", v="20"))
    original = assess(bars)
    again = assess(bars, signal_observed_ms=26 * 60000 + 1, as_of_ms=26 * 60000 + 1)
    assert original["opportunity_token"] == again["opportunity_token"]
    continued = assess(
        bars + (bar(26, o="101.5", h="103", l="101", c="102.5", v="20"),)
    )
    assert continued["status"] == "READY"
    assert continued["opportunity_token"] == original["opportunity_token"]


def test_signal_must_be_after_exit_and_after_confirmation():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    assert (
        assess(bars, signal_observed_ms=25 * 60000)["reason"] == "SIGNAL_PREDATES_EXIT"
    )
    assert (
        assess(bars, signal_observed_ms=26 * 60000 - 1)["reason"]
        == "SIGNAL_PREDATES_CONFIRMATION"
    )


def test_low_volume_or_excessive_chase_are_not_new_opportunity():
    assert assess(history(bar(25, h="102", c="101.5", v="5")))["status"] == "WAIT"
    assert assess(history(bar(25, h="111", c="110", v="20")))["status"] == "WAIT"


def test_long_short_symmetry():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    mirrored = tuple(
        Bar(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v) for b in bars
    )
    assert assess(mirrored, side="SELL")["path"] == assess(bars)["path"]


def test_small_price_scaling_preserves_decision():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    factor = Decimal("1e-12")
    scaled = tuple(
        Bar(b.t, b.o * factor, b.h * factor, b.l * factor, b.c * factor, b.v)
        for b in bars
    )
    assert assess(scaled, exit_price=str(100 * factor))["path"] == assess(bars)["path"]


def test_unknown_exit_and_insufficient_or_stale_history():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    assert assess(bars, exit_reason=None)["status"] == "UNKNOWN"
    assert assess(bars[20:])["reason"] == "ANCHOR_HISTORY_INCOMPLETE"
    assert assess(bars, as_of_ms=27 * 60000)["reason"] == "HISTORY_STALE"


def test_exit_straddling_bar_cannot_prove_repair():
    bars = history(bar(25, h="103", l="98", c="102", v="20"))
    assert (
        assess(bars, exit_known_ms=25 * 60000 + 1)["reason"]
        == "NO_POST_EXIT_CLOSED_BAR"
    )


@pytest.mark.parametrize(
    "changes", [{"as_of_ms": True}, {"signal_observed_ms": 999999999}, {"side": "LONG"}]
)
def test_bad_clock_and_side(changes):
    with pytest.raises(ValueError):
        assess(history(bar(25)), **changes)


def test_gap_future_duplicate_and_out_of_order_bars_rejected():
    bars = history(bar(25))
    for data in (bars[:3] + bars[4:], bars + (bars[-1],), tuple(reversed(bars))):
        with pytest.raises(ValueError):
            assess(data)
    with pytest.raises(ValueError, match="UNCLOSED"):
        assess(bars, as_of_ms=bars[-1].t + 59999, signal_observed_ms=bars[-1].t)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeframe_minutes": True},
        {"timeframe_minutes": 0},
        {"anchor_bars": 1},
        {"min_relative_volume": "NaN"},
        {"reset_atr": "0.1", "resume_buffer_atr": "0.1"},
    ],
)
def test_policy_bounds(changes):
    with pytest.raises(ValueError):
        replace(ReentryPolicy(), **changes)


def test_inputs_immutable_and_config_digest_changes():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    original = deepcopy(bars)
    assert (
        assess(bars)["policy_digest"]
        != assess(bars, policy=ReentryPolicy(min_relative_volume="1.1"))[
            "policy_digest"
        ]
    )
    assert bars == original


def test_old_peak_does_not_count_as_new_pullback():
    bars = history(bar(25), bar(26, h="101.3", c="101.25", v="20"))
    assert assess(bars)["status"] == "WAIT"
    assert assess(bars, exit_price=None)["reason"] == "EXIT_PRICE_MISSING"


def test_structure_persists_without_timer_then_invalidates_on_price():
    bars = history(bar(25, h="102", c="101.5", v="20"))
    ready = assess(bars)
    flat = tuple(
        bar(i, o="101.5", h="102", l="101.2", c="101.5") for i in range(26, 100)
    )
    later = assess(bars + flat)
    assert (
        later["status"] == "READY"
        and later["opportunity_token"] == ready["opportunity_token"]
    )
    broken = assess(bars + flat + (bar(100, o="101.5", h="101.5", l="99", c="100"),))
    assert broken["status"] == "WAIT"


def test_later_chase_does_not_get_fresh_token():
    bars = history(
        bar(25, h="102", c="101.5", v="20"),
        bar(26, o="101.5", h="110", l="101", c="109", v="20"),
    )
    assert assess(bars)["reason"] == "STRUCTURE_CHASE_TOO_FAR"
