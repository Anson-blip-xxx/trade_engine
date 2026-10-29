"""Model accounting, causal stop timing and no-execution authority contracts."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from services.v2_trend_runner_shadow import compare_candidates
from v2_core.trend_runner import HOUR, TrendRunnerPolicy, run_shadow


def bar(i, close, *, open_=None, high=None, low=None, available=None):
    c = Decimal(str(close))
    o = c if open_ is None else Decimal(str(open_))
    h = max(o, c) + 1 if high is None else Decimal(str(high))
    l = min(o, c) - 1 if low is None else Decimal(str(low))
    return {
        "t": i * HOUR,
        "o": str(o),
        "h": str(h),
        "l": str(l),
        "c": str(c),
        "v": "100",
        "available_at_ms": (i + 1) * HOUR if available is None else available,
    }


def scenario(side="BUY", *, hours=4, timely=True):
    sign = 1 if side == "BUY" else -1
    history = [bar(i, 100 + sign * i) for i in range(24)]
    forward = [
        bar(24 + i, 124 + sign * i if side == "BUY" else 76 - i) for i in range(hours)
    ]
    payload = {
        "evidence_mode": "TIMELY" if timely else "RETROSPECTIVE",
        "side": side,
        "activation_at_ms": 24 * HOUR,
        "as_of_ms": (24 + hours) * HOUR,
        "entry_fills": [
            {
                "at_ms": 23 * HOUR,
                "quantity": "2",
                "price": "122" if side == "BUY" else "78",
            }
        ],
        "reduction_fills": [
            {
                "at_ms": 23 * HOUR + 1000,
                "quantity": "0.5",
                "price": "123" if side == "BUY" else "77",
            }
        ],
        "native_stop": {
            "price": "110" if side == "BUY" else "90",
            "confirmed_at_ms": 23 * HOUR,
            "available_at_ms": 23 * HOUR,
            "protected_quantity": "2",
        },
        "entry_evidence": {
            "direction": side,
            "observed_at_ms": 22 * HOUR,
            "available_at_ms": 22 * HOUR,
            "digest": "a" * 64,
        },
        "history": history,
        "forward": forward,
        "funding": [],
        "funding_complete": True,
    }
    return payload


def run(payload=None, **policy):
    return run_shadow(
        scenario() if payload is None else payload,
        policy=TrendRunnerPolicy(mode="SHADOW", **policy),
    )


def dec(value):
    return Decimal(value)


def test_frozen_policy_off_default_no_live_mode():
    assert run_shadow(None)["status"] == "OFF"
    with pytest.raises(ValueError, match="NO_LIVE"):
        TrendRunnerPolicy(mode="LIVE")
    with pytest.raises(FrozenInstanceError):
        TrendRunnerPolicy().mode = "SHADOW"
    result = run()
    assert result["execution_authorized"] is False
    assert result["profitability_valid"] is False
    assert result["actual_pnl"] is False
    assert result["timeliness"] == "TIMESTAMP_VALIDATED"
    assert len(result["policy_digest"]) == len(result["input_digest"]) == 64
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_partial_filled_quantity_and_independent_never_loosened_stops(side):
    payload = scenario(side, hours=10)
    result = run(payload)
    assert result["status"] == "SHADOW"
    assert result["filled_quantity"] == "2"
    assert result["reduced_quantity"] == "0.5"
    assert result["residual_quantity"] == "1.5"
    native = dec(payload["native_stop"]["price"])
    for state in result["arms"].values():
        stops = [dec(t["stop_effective"]) for t in state["trace"]]
        assert stops == sorted(stops, reverse=side == "SELL")
        assert all(s >= native if side == "BUY" else s <= native for s in stops)
    assert (
        result["arms"]["baseline_wave"]["trace"]
        != result["arms"]["proposed_runner"]["trace"]
    )


@pytest.mark.parametrize("side,opening", [("BUY", "100"), ("SELL", "100")])
def test_adverse_gap_exits_at_open_not_better_stop_and_ignores_later_funding(
    side, opening
):
    payload = scenario(side, hours=1)
    payload["forward"] = [bar(24, opening)]
    payload["funding"] = [
        {
            "at_ms": 24 * HOUR + 1000,
            "rate": "0.01" if side == "BUY" else "-0.01",
            "mark_price": opening,
        }
    ]
    result = run(payload)
    for state in result["arms"].values():
        assert state["exit_reason"] == "STOP_GAP"
        assert (
            dec(state["exit_price"]) < 100
            if side == "BUY"
            else dec(state["exit_price"]) > 100
        )
        assert state["reported_funding_cashflow"] == "0"
        assert dec(state["modeled_fees"]) > 0
    assert result["comparison_complete"] is True


def test_same_bar_high_never_retroactively_ratchets_stop_and_restart_replay_identical():
    payload = scenario(hours=2)
    payload["forward"] = [
        bar(24, 125, open_=124, high=160, low=123),
        bar(25, 130, open_=130, high=131, low=129),
    ]
    before = deepcopy(payload)
    result = run(payload)
    wave = result["arms"]["baseline_wave"]
    assert wave["trace"][0]["status"] == "OPEN"
    assert dec(wave["trace"][0]["stop_effective"]) == 110
    assert wave["trace"][1]["reason"] == "STOP_GAP"
    assert result["arms"]["proposed_runner"]["status"] == "OPEN"
    assert result == run(payload)
    assert payload == before


def test_mutating_future_bar_cannot_change_prior_trace_or_activation():
    payload = scenario(hours=4)
    first = run(payload)
    payload["forward"][-1] = bar(27, 200, high=300, low=126)
    second = run(payload)
    assert first["activation_evidence"] == second["activation_evidence"]
    for name in first["arms"]:
        assert first["arms"][name]["trace"][:3] == second["arms"][name]["trace"][:3]


def test_missing_timely_proofs_cannot_be_silently_promoted_from_retrospective():
    payload = scenario(timely=False)
    payload["entry_evidence"] = None
    payload["native_stop"]["confirmed_at_ms"] = None
    payload["native_stop"]["available_at_ms"] = None
    for row in payload["history"] + payload["forward"]:
        row["available_at_ms"] = None
    result = run(payload)
    assert result["status"] == "SHADOW"
    assert result["timeliness"] == "NOT_PROVEN"
    assert result["native_protection_proven"] is False
    assert result["profitability_valid"] is False
    payload["evidence_mode"] = "TIMELY"
    assert run(payload)["reason"] == "FROZEN_PRE_ENTRY_EVIDENCE_MISSING"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p["history"].pop(), "HOURLY_TREND_WARMUP"),
        (lambda p: p["history"][0].update(t=1), "UNALIGNED_OR_UNCLOSED_HOURLY_BAR"),
        (lambda p: p["forward"].pop(0), "FORWARD_HISTORY_GAP"),
        (lambda p: p["forward"][1].update(t=26 * HOUR), "HOURLY_HISTORY_GAP"),
        (
            lambda p: p["history"][-1].update(available_at_ms=25 * HOUR),
            "BAR_NOT_AVAILABLE_AS_OF",
        ),
        (
            lambda p: p["history"][-1].update(available_at_ms=23 * HOUR),
            "BAR_AVAILABLE_BEFORE_CLOSE",
        ),
        (
            lambda p: p["history"][-1].update(available_at_ms=None),
            "BAR_AVAILABILITY_NOT_PROVEN",
        ),
        (
            lambda p: p["entry_evidence"].update(available_at_ms=24 * HOUR),
            "ENTRY_EVIDENCE_NOT_PRE_ENTRY",
        ),
        (
            lambda p: p["entry_evidence"].update(observed_at_ms=0, available_at_ms=0),
            "ENTRY_EVIDENCE_STALE",
        ),
        (
            lambda p: p["entry_evidence"].update(direction="SELL"),
            "ENTRY_EVIDENCE_DIRECTION_CONFLICT",
        ),
        (
            lambda p: p["native_stop"].update(available_at_ms=None),
            "NATIVE_PROTECTION_NOT_PROVEN",
        ),
        (
            lambda p: p["native_stop"].pop("protected_quantity"),
            "NATIVE_PROTECTION_QUANTITY_NOT_PROVEN",
        ),
        (
            lambda p: p["native_stop"].update(protected_quantity="1"),
            "NATIVE_PROTECTION_RESIDUAL_NOT_COVERED",
        ),
        (
            lambda p: p["native_stop"].update(price="123"),
            "ORIGINAL_STOP_NOT_LOSS_BOUNDARY",
        ),
        (
            lambda p: p["entry_fills"][0].update(at_ms=25 * HOUR),
            "FILL_NOT_AVAILABLE_AT_ACTIVATION",
        ),
        (
            lambda p: p["reduction_fills"][0].update(quantity="2"),
            "NO_POSITIVE_FILLED_RESIDUAL",
        ),
        (
            lambda p: p["reduction_fills"][0].update(at_ms=22 * HOUR),
            "INTERLEAVED_SCALE_IN_ALLOCATION_UNSUPPORTED",
        ),
        (
            lambda p: p.update(activation_at_ms=24 * HOUR + 1),
            "ACTIVATION_NOT_CLOSED_HOUR_BOUNDARY",
        ),
    ],
)
def test_missing_invalid_or_time_travelling_inputs_are_explicit_unavailable(
    mutation, reason
):
    payload = scenario()
    mutation(payload)
    result = run(payload)
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == reason
    assert result["execution_authorized"] is False


def test_low_volatility_or_opposing_activation_trend_is_ineligible():
    payload = scenario()
    payload["history"] = [bar(i, 123) for i in range(24)]
    assert run(payload)["reason"] == "ACTIVATION_TREND_NOT_CONFIRMED"


def test_missing_forward_data_never_claims_close_and_cannot_hold_indefinitely():
    payload = scenario(hours=0)
    payload["as_of_ms"] += 2 * HOUR
    result = run(payload)
    assert all(
        s["status"] == "EXIT_REQUIRED_DATA_STALE" for s in result["arms"].values()
    )
    assert all(
        "modeled_net_with_reported_funding" not in s for s in result["arms"].values()
    )
    assert result["modeled_net_delta_with_reported_funding"] is None


def test_hard_max_hold_is_enforced_from_original_entry_not_runner_activation():
    payload = scenario(hours=4)
    result = run(payload, max_hold_hours=2)
    assert all(s["exit_reason"] == "MAX_HOLD" for s in result["arms"].values())
    assert all(s["exit_bar_open_ms"] == 25 * HOUR for s in result["arms"].values())


def test_delayed_closed_bar_cannot_update_stop_before_available():
    payload = scenario(hours=3)
    payload["forward"][0] = bar(
        24, 125, open_=124, high=160, low=123, available=26 * HOUR
    )
    result = run(payload)
    wave = result["arms"]["baseline_wave"]
    assert wave["trace"][1]["stop_effective"] == "110"
    assert wave["trace"][2]["reason"] == "STOP_GAP"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_continuously_delayed_observations_are_queued_not_lost(side):
    payload = scenario(side, hours=3)
    payload["as_of_ms"] = 29 * HOUR
    for row in payload["forward"]:
        row["available_at_ms"] = row["t"] + 2 * HOUR
    before = deepcopy(payload)
    result = run(payload)
    for state in result["arms"].values():
        assert state["trace"][2]["bar_open_ms"] == 26 * HOUR
        assert state["trace"][2]["status"] == "OPEN"
        assert state["trace"][2]["reason"] is None
        assert state["status"] == "EXIT_REQUIRED_DATA_STALE"
        assert "exit_price" not in state
    assert run(payload) == result  # Deterministic recovery from the same evidence.
    assert payload == before


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_continuous_one_hour_delivery_delay_does_not_trip_stale_exit(side):
    payload = scenario(side, hours=6)
    for row in payload["forward"]:
        row["available_at_ms"] = row["t"] + 2 * HOUR
    payload["as_of_ms"] += HOUR
    result = run(payload)
    for state in result["arms"].values():
        assert state["status"] == "OPEN"
        assert all(step["reason"] != "STALE_DATA" for step in state["trace"])
        # First delayed bar cannot be effective before its actual availability.
        assert (
            state["trace"][0]["stop_effective"] == state["trace"][1]["stop_effective"]
        )
    prefix = deepcopy(payload)
    prefix["forward"] = prefix["forward"][:2]
    prefix["as_of_ms"] = 27 * HOUR
    early = run(prefix)
    for name in result["arms"]:
        assert result["arms"][name]["trace"][:2] == early["arms"][name]["trace"]


def test_intrahour_deadline_never_invents_close_price():
    payload = scenario(hours=3)
    payload["entry_fills"][0]["at_ms"] += 500
    payload["native_stop"]["confirmed_at_ms"] += 500
    payload["native_stop"]["available_at_ms"] += 500
    result = run(payload, max_hold_hours=2)
    assert all(s["status"] == "EXIT_REQUIRED_MAX_HOLD" for s in result["arms"].values())
    assert all("exit_price" not in s for s in result["arms"].values())
    assert result["modeled_net_delta_with_reported_funding"] is None


@pytest.mark.parametrize("side,rate", [("BUY", "0.001"), ("SELL", "-0.001")])
def test_funding_cost_and_incomplete_costs_are_not_hidden(side, rate):
    payload = scenario(side, hours=2)
    payload["forward"][1] = bar(25, 100)
    payload["funding"] = [{"at_ms": 24 * HOUR, "rate": rate, "mark_price": "100"}]
    result = run(payload)
    for state in result["arms"].values():
        assert dec(state["reported_funding_cashflow"]) == Decimal("-0.15")
        assert dec(state["modeled_net_with_reported_funding"]) < dec(
            state["modeled_gross"]
        )
    payload["funding_complete"] = False
    incomplete = run(payload)
    assert incomplete["modeled_net_delta_with_reported_funding"] is None
    assert incomplete["exit_comparison_complete"] is True
    assert incomplete["cost_comparison_complete"] is False
    assert incomplete["comparison_complete"] is False
    for state in incomplete["arms"].values():
        assert state["modeled_net_with_reported_funding"] is None
        assert state["funding_completeness"] == "PARTIAL_NOT_TOTAL"
        assert dec(state["modeled_after_fees_before_funding"]) == dec(
            state["modeled_gross"]
        ) - dec(state["modeled_fees"])


def test_unknown_funding_is_not_zero_net_even_when_no_rows_were_reported():
    payload = scenario(hours=1)
    payload["forward"] = [bar(24, 100)]
    payload["funding_complete"] = False
    result = run(payload)
    for state in result["arms"].values():
        assert state["reported_funding_cashflow"] == "0"
        assert state["funding_completeness"] == "PARTIAL_NOT_TOTAL"
        assert state["modeled_net_with_reported_funding"] is None
        assert dec(state["modeled_after_fees_before_funding"]) < 0
    assert result["exit_comparison_complete"] is True
    assert result["comparison_complete"] is False


def test_fill_weighted_price_and_proportional_remaining_basis():
    payload = scenario()
    payload["entry_fills"] = [
        {"at_ms": 23 * HOUR, "quantity": "1", "price": "120"},
        {"at_ms": 23 * HOUR + 100, "quantity": "1", "price": "124"},
    ]
    result = run(payload)
    assert result["entry_price"] == "122"
    assert result["residual_quantity"] == "1.5"


@pytest.mark.parametrize(
    "field,value",
    [
        ("slow_period", True),
        ("max_stale_hours", 0),
        ("atr_multiple", "NaN"),
        ("fee_rate", "-0.1"),
        ("exit_slippage_fraction", "1"),
        ("atr_period", 1),
        ("baseline_trail_fraction", 0.1),
    ],
)
def test_policy_bounds_reject_invalid_or_unbounded_values(field, value):
    with pytest.raises(ValueError):
        replace(TrendRunnerPolicy(), **{field: value})


def test_batch_is_bounded_and_bad_candidate_cannot_block_next():
    result = compare_candidates(
        [{}, scenario()], policy=TrendRunnerPolicy(mode="SHADOW")
    )
    assert result["reports"][0]["report"]["reason"] == "INVALID_CANDIDATE_CONTRACT"
    assert result["reports"][1]["report"]["status"] == "SHADOW"
    with pytest.raises(ValueError, match="BATCH_BOUND"):
        compare_candidates([scenario()] * 101)
    with pytest.raises(ValueError, match="BATCH_BOUND"):
        compare_candidates([], max_candidates=True)


def test_payload_shape_is_bounded_before_digest_and_batch_can_continue():
    payload = scenario()
    payload["entry_evidence"]["digest"] = {"recursive": {"arbitrary": []}}
    batch = compare_candidates(
        [payload, scenario()], policy=TrendRunnerPolicy(mode="SHADOW")
    )
    assert batch["reports"][0]["report"]["reason"] == "INVALID_CANDIDATE_CONTRACT"
    assert batch["reports"][1]["report"]["status"] == "SHADOW"


def test_declining_short_costs_still_use_adverse_buy_to_cover_price():
    payload = scenario("SELL", hours=2)
    payload["forward"][1] = bar(25, 90)
    result = run(payload, fee_rate="0.001", exit_slippage_fraction="0.01")
    arm = result["arms"]["proposed_runner"]
    assert arm["exit_reason"] == "STOP_GAP"
    assert dec(arm["exit_price"]) == Decimal("90.9")
    assert dec(arm["modeled_gross"]) == Decimal("-19.35")
    assert dec(arm["modeled_fees"]) == Decimal("0.25335")
    assert dec(arm["modeled_net_with_reported_funding"]) == Decimal("-19.60335")


def test_high_volatility_never_widens_proposed_original_or_previous_stop():
    payload = scenario(hours=3)
    payload["forward"][0] = bar(24, 124, high=200, low=120)
    payload["forward"][1] = bar(25, 125, high=210, low=122)
    payload["forward"][2] = bar(26, 126, high=230, low=124)
    result = run(payload)
    arm = result["arms"]["proposed_runner"]
    stops = [dec(t["stop_effective"]) for t in arm["trace"]]
    assert stops == sorted(stops)
    assert min(stops) >= dec(payload["native_stop"]["price"])


def test_no_network_database_model_or_execution_imports():
    import ast
    from pathlib import Path

    import services.v2_trend_runner_shadow as driver
    import v2_core.trend_runner as core

    for module in (core, driver):
        tree = ast.parse(Path(module.__file__).read_text())
        imports = [
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ]
        assert not any(
            x
            and any(
                t in x for t in ("requests", "psycopg", "binance", "jev", "execution")
            )
            for x in imports
        )
