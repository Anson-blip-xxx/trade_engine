from dataclasses import replace

import pytest

from v2_core.directional_exit import ExitFacts, evaluate_directional_exit


def facts(**changes):
    base = ExitFacts(
        system_tag="S6A",
        side="BUY",
        entry_price="100",
        mark_price="101",
        stop_price="92",
        remaining_quantity="1.25",
        planned_loss="10",
        held_ms=10 * 60000,
        funding_rate="0",
        ema9_1h="101",
        ema20_1h="100",
        momentum_closes_15m=("100", "100", "100", "101"),
        peak_return_pct="1",
        partial_done=False,
        exit_fee_rate="0.0004",
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    "changes,action,reason",
    [
        ({"funding_rate": ".0051"}, "CLOSE", "ADVERSE_FUNDING"),
        ({"mark_price": "91"}, "WAIT_NATIVE_STOP", "NATIVE_STOP_BOUNDARY"),
        (
            {
                "mark_price": "97.5",
                "held_ms": 5 * 60000,
                "momentum_closes_15m": ("100", "99", "98", "97"),
            },
            "CLOSE",
            "EARLY_LOSS_MOMENTUM_WEAK",
        ),
        (
            {"held_ms": 91 * 60000, "mark_price": "100.1"},
            "CLOSE",
            "LOW_YIELD_STAGNATION",
        ),
        ({"mark_price": "105"}, "PARTIAL", "PARTIAL_TAKE_PROFIT"),
        (
            {"mark_price": "102", "peak_return_pct": "5", "partial_done": True},
            "CLOSE",
            "PEAK_DRAWDOWN",
        ),
        (
            {
                "held_ms": 60 * 60000,
                "ema9_1h": "97",
                "ema20_1h": "100",
            },
            "CLOSE",
            "ONE_HOUR_REVERSAL",
        ),
        (
            {"held_ms": 121 * 60000, "mark_price": "99"},
            "CLOSE",
            "TIME_STOP",
        ),
    ],
)
def test_exit_priority_and_reasons(changes, action, reason):
    decision = evaluate_directional_exit(facts(**changes))
    assert (decision.action, decision.reason) == (action, reason)


def test_short_signs_momentum_reversal_and_funding_are_symmetric():
    short = facts(
        system_tag="S8",
        side="SELL",
        mark_price="102.5",
        stop_price="108",
        funding_rate="-.006",
        momentum_closes_15m=("100", "101", "102", "103"),
    )
    assert evaluate_directional_exit(short).reason == "ADVERSE_FUNDING"
    assert evaluate_directional_exit(replace(short, funding_rate="0")).reason == (
        "EARLY_LOSS_MOMENTUM_WEAK"
    )
    reversal = replace(
        short,
        mark_price="99",
        held_ms=60 * 60000,
        funding_rate="0",
        ema9_1h="103",
        ema20_1h="100",
        momentum_closes_15m=("100", "100", "100", "99"),
    )
    assert evaluate_directional_exit(reversal).reason == "ONE_HOUR_REVERSAL"


def test_partial_threshold_and_ratio_follow_original_system_tag():
    assert evaluate_directional_exit(facts(mark_price="105")).quantity_fraction == "0.5"
    s6b = facts(system_tag="S6B", mark_price="108")
    assert evaluate_directional_exit(s6b).quantity_fraction == "0.3"
    assert evaluate_directional_exit(replace(s6b, mark_price="107.99")).action == "WAIT"


def test_peak_is_monotonic_and_cost_is_included_in_risk_multiple():
    decision = evaluate_directional_exit(facts(mark_price="102", peak_return_pct="1"))
    assert decision.peak_return_pct == "2"
    assert decision.gross_pnl == "2.5"
    assert decision.estimated_net_pnl == "2.449"
    assert decision.risk_multiple == "0.2449"


@pytest.mark.parametrize(
    "changes",
    [
        {"system_tag": "S8B"},
        {"side": "LONG"},
        {"held_ms": -1},
        {"stop_price": "101"},
        {"momentum_closes_15m": ("1", "2", "3")},
        {"exit_fee_rate": ".02"},
    ],
)
def test_malformed_or_unmapped_facts_fail_closed(changes):
    with pytest.raises((ValueError, TypeError)):
        evaluate_directional_exit(facts(**changes))
