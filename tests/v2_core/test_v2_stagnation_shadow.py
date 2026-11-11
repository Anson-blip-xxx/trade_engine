"""Frozen exit qualification only; no process, database, venue or model ports."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from services.v2_trend_runner_shadow import compare_stagnation_candidates
from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.runtime_policy import resolve
from v2_core.trend_runner import HOUR, StagnationShadowPolicy, stagnation_shadow


def snapshot(side="BUY", *, at=24 * HOUR + 300000, opened=None):
    opened = at - 90 * 60000 if opened is None else opened
    sign = 1 if side == "BUY" else -1
    facts = ExitFacts(
        system_tag="S8",
        side=side,
        entry_price="100",
        mark_price="100.5" if side == "BUY" else "99.5",
        stop_price="95" if side == "BUY" else "105",
        remaining_quantity="1",
        planned_loss="5",
        held_ms=at - opened,
        funding_rate="0",
        ema9_1h="100" if side == "BUY" else "99",
        ema20_1h="99" if side == "BUY" else "100",
        momentum_closes_15m=("100", "100.1", "100.2", "100.3")
        if side == "BUY"
        else ("100", "99.9", "99.8", "99.7"),
        peak_return_pct="0.5",
        partial_done=False,
        exit_fee_rate="0.0005",
    )
    history = []
    for hour in range(24):
        close = 80 + hour if sign == 1 else 120 - hour
        history.append(
            {
                "t": hour * HOUR,
                "o": str(close),
                "h": str(close + 1),
                "l": str(close - 1),
                "c": str(close),
                "v": "100",
                "available_at_ms": (hour + 1) * HOUR,
            }
        )
    values = resolve()
    return {
        "at_ms": at,
        "facts": {
            **asdict(facts),
            "momentum_closes_15m": list(facts.momentum_closes_15m),
        },
        "baseline": evaluate_directional_exit(facts, policy=values).evidence(),
        "baseline_policy": values,
        "observation": {
            "observed_at_ms": at,
            "available_at_ms": at,
            "closed_15m_at_ms": at // 900000 * 900000,
            "closed_1h_at_ms": at // HOUR * HOUR,
            "environment": "SANDBOX",
            "market_source": "BINANCE_USDM_SANDBOX",
        },
        "native_protection": {
            "status": "NEW",
            "price": facts.stop_price,
            "protected_quantity": "1",
            "observed_at_ms": at,
            "available_at_ms": at,
        },
        "hourly_history": history,
    }


def scenario(side="BUY"):
    row = snapshot(side)
    return {"opened_at_ms": row["at_ms"] - row["facts"]["held_ms"], "snapshots": [row]}


def recalculate(row):
    row["baseline"] = evaluate_directional_exit(
        ExitFacts(**row["facts"]), policy=row["baseline_policy"]
    ).evidence()


def run(payload=None, **overrides):
    return stagnation_shadow(
        scenario() if payload is None else payload,
        policy=StagnationShadowPolicy(mode="SHADOW", **overrides),
    )


def test_default_off_does_not_inspect_input_and_live_mode_is_impossible():
    assert stagnation_shadow(None)["status"] == "OFF"
    with pytest.raises(ValueError, match="NO_LIVE"):
        StagnationShadowPolicy(mode="LIVE")


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_timely_trend_and_momentum_can_qualify_only_as_a_research_prefix(side):
    payload = scenario(side)
    before = deepcopy(payload)
    result = run(payload)
    assert result["status"] == "SHADOW_ELIGIBLE_PREFIX"
    assert result["continuation_eligibility"] == "ELIGIBLE_OBSERVED_PREFIX_ONLY"
    assert result["baseline_reproduced_count"] == 1
    assert result["trace"][0]["baseline_reproduced"] is True
    assert result["extension_deadline_ms"] == payload["snapshots"][0]["at_ms"] + HOUR
    assert result["execution_authorized"] is result["promotion_authorized"] is False
    assert result["profitability_valid"] is result["actual_pnl"] is False
    assert result["baseline_code_identity_proven"] is False
    assert result["reason"] == "FORWARD_OUTCOME_NOT_PROVEN"
    assert run(payload) == result
    assert payload == before


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda r: r.update(native_protection=None),
            "FROZEN_NATIVE_PROTECTION_MISSING",
        ),
        (
            lambda r: r["native_protection"].update(status="FILLED"),
            "NATIVE_PROTECTION_NOT_CURRENT_AND_COVERED",
        ),
        (
            lambda r: r["native_protection"].update(protected_quantity="0.5"),
            "NATIVE_PROTECTION_NOT_CURRENT_AND_COVERED",
        ),
        (
            lambda r: r["native_protection"].update(price="94"),
            "NATIVE_PROTECTION_NOT_CURRENT_AND_COVERED",
        ),
        (
            lambda r: r["native_protection"].update(observed_at_ms=0),
            "NATIVE_PROTECTION_NOT_CURRENT_AND_COVERED",
        ),
        (
            lambda r: r["observation"].update(available_at_ms=None),
            "OBSERVATION_AVAILABILITY_NOT_PROVEN",
        ),
        (
            lambda r: r["observation"].update(observed_at_ms=0),
            "EXIT_OBSERVATION_NOT_TIMELY",
        ),
        (
            lambda r: r["observation"].update(available_at_ms=r["at_ms"] + 1),
            "EXIT_OBSERVATION_NOT_TIMELY",
        ),
        (
            lambda r: r["observation"].update(closed_1h_at_ms=25 * HOUR),
            "CLOSED_EXIT_CANDLES_STALE_OR_FUTURE",
        ),
        (
            lambda r: r["observation"].update(closed_15m_at_ms=0),
            "CLOSED_EXIT_CANDLES_STALE_OR_FUTURE",
        ),
        (
            lambda r: r["observation"].update(market_source="BINANCE_USDM_LIVE"),
            "EXECUTION_MARKET_SOURCE_MISMATCH",
        ),
        (
            lambda r: r["hourly_history"][-1].update(available_at_ms=None),
            "BAR_AVAILABILITY_NOT_PROVEN",
        ),
        (
            lambda r: r["hourly_history"][-1].update(available_at_ms=r["at_ms"] + 1),
            "BAR_NOT_AVAILABLE_AS_OF",
        ),
        (lambda r: r["hourly_history"].pop(), "HOURLY_TREND_WARMUP_OR_STALE"),
    ],
)
def test_missing_stale_future_or_wrong_market_proof_keeps_baseline_but_never_holds(
    mutation, reason
):
    payload = scenario()
    mutation(payload["snapshots"][0])
    result = run(payload)
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == reason
    assert result["baseline_reproduced_count"] == 1
    assert result["continuation_eligibility"] == "UNKNOWN"
    assert not result["trace"][0]["continuation_eligible"]


def test_partial_baseline_policy_is_not_filled_with_current_defaults():
    payload = scenario()
    payload["snapshots"][0]["baseline_policy"].pop("exit.stagnation_minutes")
    assert run(payload)["reason"] == "FROZEN_BASELINE_POLICY_INCOMPLETE"


def test_baseline_mismatch_cannot_be_promoted_to_a_candidate():
    payload = scenario()
    payload["snapshots"][0]["baseline"]["estimated_net_pnl"] = "123"
    result = run(payload)
    assert result["reason"] == "FROZEN_BASELINE_REPLAY_MISMATCH"
    assert result["baseline_reproduced_count"] == 0


def test_only_first_stagnation_decision_can_anchor_research():
    payload = scenario()
    row = payload["snapshots"][0]
    row["facts"]["mark_price"] = "94"
    recalculate(row)
    assert run(payload)["status"] == "NOT_APPLICABLE"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda r: r["facts"].update(peak_return_pct="3.5"), "PEAK_DRAWDOWN"),
        (lambda r: r["facts"].update(ema9_1h="90"), "ONE_HOUR_REVERSAL"),
    ],
)
def test_stagnation_cannot_mask_a_later_baseline_protective_exit(mutation, reason):
    payload = scenario()
    row = payload["snapshots"][0]
    mutation(row)
    recalculate(row)
    assert row["baseline"]["reason"] == "LOW_YIELD_STAGNATION"
    result = run(payload)
    assert result["status"] == "NO_CONTINUATION"
    assert result["trace"][0]["protected_baseline_reason"] == reason


@pytest.mark.parametrize("field,value", [("mark_price", "94"), ("funding_rate", "0.1")])
def test_native_and_other_emergency_reasons_end_an_existing_prefix(field, value):
    payload = scenario()
    row = snapshot(
        at=payload["snapshots"][0]["at_ms"] + 60000, opened=payload["opened_at_ms"]
    )
    row["facts"][field] = value
    recalculate(row)
    payload["snapshots"].append(row)
    result = run(payload)
    assert result["reason"] == "PRESERVE_BASELINE_PROTECTION_OR_EXIT"
    assert result["trace"][-1]["continuation_eligible"] is False


def test_a_new_stagnation_snapshot_never_moves_the_original_deadline():
    payload = scenario()
    first = payload["snapshots"][0]["at_ms"]
    payload["snapshots"].extend(
        snapshot(at=first + i * 60000, opened=payload["opened_at_ms"])
        for i in range(1, 4)
    )
    result = run(payload, max_extension_minutes=3)
    assert result["extension_deadline_ms"] == first + 3 * 60000
    assert result["reason"] == "FIXED_EXTENSION_DEADLINE"
    assert len(result["trace"]) == 4


def test_total_holding_cap_is_measured_from_original_fill_not_first_stagnation():
    assert run(max_total_hold_minutes=90)["reason"] == "FIXED_EXTENSION_DEADLINE"


def test_missing_observation_coverage_cannot_establish_continuation():
    payload = scenario()
    payload["snapshots"].append(
        snapshot(
            at=payload["snapshots"][0]["at_ms"] + 180000, opened=payload["opened_at_ms"]
        )
    )
    assert run(payload)["reason"] == "SNAPSHOT_COVERAGE_GAP"


def test_reduction_policy_or_episode_clock_changes_are_not_silently_replayed():
    for change in ("position", "policy", "clock"):
        payload = scenario()
        row = snapshot(
            at=payload["snapshots"][0]["at_ms"] + 60000, opened=payload["opened_at_ms"]
        )
        if change == "position":
            row["facts"]["remaining_quantity"] = "0.5"
        elif change == "policy":
            row["baseline_policy"]["exit.stagnation_minutes"] = 89
        else:
            row["facts"]["held_ms"] += 1
        recalculate(row)
        payload["snapshots"].append(row)
        assert run(payload)["reason"] == (
            "ORIGINAL_HOLDING_CLOCK_MISMATCH"
            if change == "clock"
            else "POSITION_OR_BASELINE_POLICY_CHANGED"
        )


def test_opposing_closed_momentum_and_flat_hourly_structure_do_not_qualify():
    for change in ("momentum", "hourly"):
        payload = scenario()
        row = payload["snapshots"][0]
        if change == "momentum":
            row["facts"]["momentum_closes_15m"] = ["100.3", "100.2", "100.1", "100"]
            recalculate(row)
        else:
            for bar in row["hourly_history"]:
                bar.update(o="100", h="101", l="99", c="100")
        assert run(payload)["reason"] == "TREND_NOT_INTACT"


def test_future_mutation_does_not_change_earlier_evidence_or_deadline():
    payload = scenario()
    before = run(payload)
    row = snapshot(
        at=payload["snapshots"][0]["at_ms"] + 60000, opened=payload["opened_at_ms"]
    )
    row["facts"]["mark_price"] = "94"
    recalculate(row)
    payload["snapshots"].append(row)
    after = run(payload)
    assert after["trace"][:1] == before["trace"]
    assert after["extension_deadline_ms"] == before["extension_deadline_ms"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_extension_minutes", True),
        ("max_total_hold_minutes", 0),
        ("max_snapshot_gap_ms", 999999),
        ("max_observation_age_ms", 0),
    ],
)
def test_unbounded_or_invalid_policy_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(StagnationShadowPolicy(), **{field: value})


def test_batch_isolates_bad_candidates_and_does_not_expose_raw_error():
    result = compare_stagnation_candidates(
        [{"secret": "must-not-leak"}, scenario()],
        policy=StagnationShadowPolicy(mode="SHADOW"),
    )
    assert (
        result["reports"][0]["report"]["reason"] == "INVALID_FROZEN_STAGNATION_CONTRACT"
    )
    assert result["reports"][1]["report"]["status"] == "SHADOW_ELIGIBLE_PREFIX"
    assert "must-not-leak" not in str(result)
    with pytest.raises(ValueError, match="BATCH_BOUND"):
        compare_stagnation_candidates([scenario()] * 101)
    with pytest.raises(ValueError, match="BATCH_BOUND"):
        compare_stagnation_candidates([], max_candidates=True)


def test_time_stop_cannot_be_removed_by_shadow_extension():
    payload = scenario()
    payload["opened_at_ms"] -= 31 * 60000
    row = payload["snapshots"][0]
    row["facts"].update(system_tag="S6A", held_ms=121 * 60000)
    recalculate(row)
    assert row["baseline"]["reason"] == "LOW_YIELD_STAGNATION"
    result = run(payload)
    assert result["trace"][0]["protected_baseline_reason"] == "TIME_STOP"


def test_shadow_cannot_hide_an_emergency_loss_before_native_stop():
    payload = scenario()
    first = payload["snapshots"][0]
    first["facts"]["stop_price"] = "90"
    first["native_protection"]["price"] = "90"
    recalculate(first)
    row = deepcopy(first)
    row["at_ms"] += 60000
    row["facts"].update(mark_price="94.9", held_ms=row["facts"]["held_ms"] + 60000)
    recalculate(row)
    assert row["baseline"]["reason"] == "EMERGENCY_LOSS"
    payload["snapshots"].append(row)
    assert run(payload)["reason"] == "PRESERVE_BASELINE_PROTECTION_OR_EXIT"


def test_exact_freshness_boundary_is_expired():
    payload = scenario()
    row = payload["snapshots"][0]
    row["observation"]["observed_at_ms"] -= 10000
    assert run(payload)["reason"] == "EXIT_OBSERVATION_NOT_TIMELY"


def test_snapshot_order_and_quantity_bounds_are_explicit():
    payload = scenario()
    payload["snapshots"].append(deepcopy(payload["snapshots"][0]))
    assert run(payload)["reason"] == "SNAPSHOT_ORDER_INVALID"
    payload["snapshots"] = payload["snapshots"] * 129
    with pytest.raises(ValueError, match="SNAPSHOT_BOUND"):
        run(payload)


def test_changed_current_mark_cannot_use_old_hourly_alignment_to_qualify():
    payload = scenario()
    row = payload["snapshots"][0]
    # A different entry basis keeps this a positive low-yield baseline, while
    # the observed current price has crossed below the frozen hourly fast EMA.
    row["facts"].update(entry_price="98", mark_price="98.5")
    recalculate(row)
    result = run(payload)
    assert result["reason"] == "TREND_NOT_INTACT"
    assert result["trace"][0]["evidence"]["hourly_aligned"] is False


def test_custom_research_periods_are_versioned_bounded_and_not_runtime_updates():
    result = run(fast_period=5, slow_period=20, structure_period=4)
    assert result["policy"]["fast_period"] == 5
    assert result["policy_digest"] != run()["policy_digest"]
    with pytest.raises(ValueError, match="PERIOD_ORDER"):
        StagnationShadowPolicy(fast_period=24, slow_period=24)


def test_original_input_digest_covers_observation_native_proof_and_history():
    result = run()
    for section in ("observation", "native_protection"):
        payload = scenario()
        payload["snapshots"][0][section]["available_at_ms"] -= 1
        payload["snapshots"][0][section]["observed_at_ms"] -= 1
        changed = run(payload)
        assert changed["status"] == "SHADOW_ELIGIBLE_PREFIX"
        assert changed["input_digest"] != result["input_digest"]
