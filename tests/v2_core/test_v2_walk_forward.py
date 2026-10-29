"""Causal, bounded walk-forward validation using the real quant replay."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from v2_core.evidence import canonical, digest
from v2_core.quant_indicators import Bar
from v2_core.quant_registry import parameters, warmup
from v2_core.quant_replay import ReplayPolicy, replay
from v2_core.walk_forward import walk_forward

MINUTE = 60000
PARAMS = {
    "period": 2,
    "channel_period": 3,
    "band_period": 3,
    "ema_fast": 2,
    "ema_slow": 3,
    "volume_period": 3,
    "min_rvol": "1",
}
CANDIDATES = [
    {"id": "fast", "plugin": "donchian", "params": PARAMS},
    {
        "id": "slow",
        "plugin": "donchian",
        "params": {**PARAMS, "channel_period": 7},
    },
]


def bars(count=54):
    result = []
    for index in range(count):
        phase = index % 18
        price = "102" if 9 <= phase <= 13 else "100"
        center = Decimal(price)
        result.append(
            Bar(
                index * MINUTE,
                center,
                center + Decimal(".1"),
                center - Decimal(".1"),
                center,
                "30" if phase in (9, 14) else "10",
            )
        )
    return result


def run(series=None, **changes):
    return walk_forward(
        bars() if series is None else series,
        **{
            "timeframe_minutes": 1,
            "candidates": CANDIDATES,
            "train_bars": 18,
            "test_bars": 12,
            **changes,
        },
    )


def test_complete_folds_freeze_all_variants_and_independent_cash_without_execution():
    source = bars()
    candidates = deepcopy(CANDIDATES)
    before = deepcopy((source, candidates))
    result = run(source, candidates=candidates)
    assert result["status"] == "SIMULATED"
    assert result["fold_count"] == 3
    assert result["execution_authorized"] is False
    assert result["portfolio_equity"] is None
    assert result["entry_gate"]["hmm_fitted"] is False
    assert result["selection_policy"]["uses_test_data"] is False
    assert (source, candidates) == before
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    for index, fold in enumerate(result["folds"]):
        assert fold["train"]["start_index"] == 0
        assert fold["train"]["end_index_exclusive"] == 18 + index * 12
        assert fold["test"]["start_index"] == 18 + index * 12
        assert fold["test"]["end_index_exclusive"] == 30 + index * 12
        assert (
            len(fold["candidate_trials"]) == len(fold["candidate_oos_baselines"]) == 2
        )
        selection = fold["selection"]
        assert selection["parameters_digest"] == digest(canonical(selection["params"]))
        assert selection["selected_at_ms"] <= fold["test"]["start_ms"]
        assert fold["out_of_sample"]["candidate_id"] == selection["id"]
        assert fold["cash_baseline"]["final_equity"] == "1000"
        assert fold["policy"]["fee_rate"] == ReplayPolicy().fee_rate
        for baseline in fold["candidate_oos_baselines"]:
            assert baseline["starts_flat"] is True
            assert baseline["initial_equity"] == "1000"
            assert baseline["execution_authorized"] is False
            assert len(baseline["replay"]["curve"]) == 12
            assert all(
                fold["test"]["start_ms"]
                <= entry["opened_at_ms"]
                < fold["test"]["end_ms_exclusive"]
                for entry in baseline["replay"]["entries"]
            )
            assert all(
                fold["test"]["start_ms"]
                < point["closed_at_ms"]
                <= fold["test"]["end_ms_exclusive"]
                for point in baseline["replay"]["curve"]
            )
        assert fold["execution_authorized"] is False


def test_future_suffix_cannot_change_prior_fold_selection_results_or_hashes():
    source = bars(66)
    early = run(source[:42])
    altered = source[:42] + [
        Bar(bar.t, "50", "80", "40", "60", "1000000") for bar in source[42:]
    ]
    late = run(altered)
    assert early["folds"] == late["folds"][:2]
    assert early["input_digest"] != late["input_digest"]


def test_first_fold_selection_never_reads_its_test_even_when_test_changes():
    source = bars(30)
    first = run(source)
    altered = source[:18] + [
        Bar(bar.t, "100", "101", "99", "100", "1") for bar in source[18:]
    ]
    second = run(altered)
    assert (
        first["folds"][0]["candidate_trials"] == second["folds"][0]["candidate_trials"]
    )
    assert first["folds"][0]["selection"] == second["folds"][0]["selection"]
    assert (
        first["folds"][0]["candidate_oos_baselines"]
        != second["folds"][0]["candidate_oos_baselines"]
    )


def test_training_is_sliced_before_gap_and_test_and_replays_with_exact_inputs():
    source = bars(36)
    result = run(source, gap_bars=3)
    fold = result["folds"][0]
    assert fold["train"]["end_ms_exclusive"] + 3 * MINUTE == fold["test"]["start_ms"]
    for candidate, trial in zip(
        result["candidates"], fold["candidate_trials"], strict=True
    ):
        expected = replay(
            source[:18],
            timeframe_minutes=1,
            plugin=candidate["plugin"],
            params=candidate["params"],
            entry_start_ms=0,
            entry_end_ms=18 * MINUTE,
        )
        assert trial["replay"] == expected
        assert trial["input_digest"] == digest(
            canonical({"bars": [bar.payload() for bar in source[:18]], "funding": []})
        )
    for candidate, baseline in zip(
        result["candidates"], fold["candidate_oos_baselines"], strict=True
    ):
        seed = 21 - warmup(candidate["params"])
        assert baseline["input_start_ms"] == source[seed].t
        assert baseline["entry_start_ms"] == source[21].t
        assert baseline["entry_end_ms_exclusive"] == 33 * MINUTE
        assert all(
            attempt["attempted_at_ms"] >= 21 * MINUTE
            for attempt in baseline["replay"]["entry_attempts"]
        )


def test_training_position_cannot_leak_into_test_or_be_forced_closed():
    source = bars(14)
    result = run(source, train_bars=11, test_bars=3)
    fold = result["folds"][0]
    train = fold["candidate_trials"][0]
    assert train["replay"]["open_position"] is not None
    assert train["replay"]["summary"]["closed_trades"] == 0
    assert train["replay"]["summary"]["realized_net_pnl"] == "0"
    assert "OPEN_POSITION_AT_BOUNDARY" in train["flags"]
    assert fold["candidate_oos_baselines"][0]["replay"]["open_position"] is None
    assert fold["candidate_oos_baselines"][0]["replay"]["summary"]["cash"] == "1000"


def test_oos_open_position_is_marked_not_presented_as_realized_profit():
    source = bars(12)
    fold = run(source, train_bars=10, test_bars=2)["folds"][0]
    baseline = fold["candidate_oos_baselines"][0]
    position = baseline["replay"]["open_position"]
    assert position is not None
    assert position["opened_at_ms"] == 10 * MINUTE
    assert baseline["replay"]["summary"]["closed_trades"] == 0
    assert baseline["replay"]["summary"]["realized_net_pnl"] == "0"
    assert Decimal(baseline["net_marked_pnl"]) < 0
    assert "OPEN_POSITION_AT_BOUNDARY" in baseline["flags"]


def test_fees_and_slippage_change_actual_simulation_and_frozen_policy_hash():
    source = bars(12)
    cheap = run(
        source,
        train_bars=10,
        test_bars=2,
        policy=ReplayPolicy(fee_rate="0", slippage_fraction="0"),
    )
    costly = run(
        source,
        train_bars=10,
        test_bars=2,
        policy=ReplayPolicy(fee_rate="0.001", slippage_fraction="0.001"),
    )
    left = cheap["folds"][0]["candidate_oos_baselines"][0]["replay"]
    right = costly["folds"][0]["candidate_oos_baselines"][0]["replay"]
    assert cheap["policy_digest"] != costly["policy_digest"]
    assert Decimal(right["summary"]["fees"]) > Decimal(left["summary"]["fees"])
    assert Decimal(right["entries"][0]["entry_price"]) > Decimal(
        left["entries"][0]["entry_price"]
    )
    assert Decimal(right["summary"]["equity"]) < Decimal(left["summary"]["equity"])


def test_selection_uses_training_marked_pnl_and_ties_are_stable_by_id():
    quiet = [
        Bar(index * MINUTE, "100", "100", "100", "100", "0") for index in range(30)
    ]
    result = run(quiet, candidates=list(reversed(CANDIDATES)))
    fold = result["folds"][0]
    assert fold["selection"]["id"] == "fast"
    assert fold["selection"]["training_score"] == "0"
    assert all("NO_ENTRIES" in trial["flags"] for trial in fold["candidate_trials"])
    assert all(
        "NO_CLOSED_TRADES" in baseline["flags"]
        for baseline in fold["candidate_oos_baselines"]
    )
    assert (
        len({candidate["candidate_digest"] for candidate in result["candidates"]}) == 2
    )
    assert result["candidates"][0]["params"] == parameters(PARAMS)


def test_higher_training_return_wins_over_id_and_variant_results_are_all_retained():
    candidates = [
        {
            "id": "a-long-target",
            "plugin": "donchian",
            "params": {**PARAMS, "target_r_multiple": "20"},
        },
        {
            "id": "z-short-target",
            "plugin": "donchian",
            "params": {**PARAMS, "target_r_multiple": "0.01"},
        },
    ]
    result = run(
        bars(30),
        candidates=candidates,
        policy=ReplayPolicy(fee_rate="0", slippage_fraction="0"),
    )
    fold = result["folds"][0]
    trials = {trial["candidate_id"]: trial for trial in fold["candidate_trials"]}
    assert Decimal(trials["z-short-target"]["net_marked_pnl"]) > Decimal(
        trials["a-long-target"]["net_marked_pnl"]
    )
    assert fold["selection"]["id"] == "z-short-target"
    assert (
        fold["selection"]["training_score"]
        == trials["z-short-target"]["net_marked_pnl"]
    )
    assert fold["selection"]["params"]["target_r_multiple"] == "0.01"
    assert {
        baseline["candidate_id"] for baseline in fold["candidate_oos_baselines"]
    } == set(trials)


@pytest.mark.parametrize("count", [0, 5, 18, 29])
def test_insufficient_complete_oos_is_explicit_and_never_invents_future(count):
    result = run(bars(count))
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["folds"] == []
    assert "NO_COMPLETE_OOS_FOLD" in result["flags"]
    assert result["required_bars_for_first_fold"] == 30
    assert result["work"]["candidate_replay_calls"] == 0
    assert result["execution_authorized"] is False


def test_partial_tail_is_reported_and_does_not_change_complete_fold():
    full = run(bars(30))
    partial = run(bars(35))
    assert partial["unevaluated_tail_bars"] == 5
    assert "PARTIAL_OOS_NOT_EVALUATED" in partial["flags"]
    assert partial["folds"] == full["folds"]


def test_no_candidate_with_training_warmup_can_be_selected():
    result = run(bars(10), train_bars=3, test_bars=7)
    fold = result["folds"][0]
    assert fold["status"] == "INSUFFICIENT_TRAIN_DATA"
    assert fold["selection"] is fold["out_of_sample"] is None
    assert len(fold["candidate_oos_baselines"]) == 2
    assert all(
        "INSUFFICIENT_TRAIN_WARMUP" in trial["flags"]
        for trial in fold["candidate_trials"]
    )


def test_funding_is_period_scoped_and_never_claimed_complete():
    source = bars(12)
    events = [
        {"at_ms": index * MINUTE, "rate": "0.001", "mark_price": "102"}
        for index in (9, 10, 11)
    ]
    result = run(source, train_bars=10, test_bars=2, funding=events)
    assert result["funding_complete"] is False
    assert result["flags"] == ["FUNDING_COMPLETENESS_UNVERIFIED"]
    fold = result["folds"][0]
    assert fold["candidate_trials"][0]["funding_event_count"] == 1
    baseline = fold["candidate_oos_baselines"][0]
    assert baseline["funding_event_count"] == 2
    assert baseline["funding_complete"] is False
    assert Decimal(baseline["replay"]["summary"]["funding"]) < 0
    assert "FUNDING_MISSING" in run(source, train_bars=10, test_bars=2)["flags"]


@pytest.mark.parametrize(
    "changes",
    [
        {"train_bars": True},
        {"train_bars": 0},
        {"test_bars": False},
        {"test_bars": 0},
        {"test_bars": "12"},
        {"gap_bars": True},
        {"gap_bars": -1},
        {"gap_bars": 10001},
        {"timeframe_minutes": True},
        {"timeframe_minutes": 2},
        {"policy": False},
        {"policy": {}},
        {"candidates": []},
        {"candidates": CANDIDATES * 5},
        {"candidates": [CANDIDATES[0], CANDIDATES[0]]},
        {"candidates": [{"id": "x", "plugin": "unknown", "params": {}}]},
        {"candidates": [{"id": [], "plugin": "donchian", "params": {}}]},
        {"candidates": [{"id": "x", "plugin": "donchian", "params": {"period": True}}]},
        {
            "candidates": [
                {
                    "id": "x",
                    "plugin": "donchian",
                    "params": {"trend_regime_required": 1},
                }
            ]
        },
    ],
)
def test_malformed_bounds_and_candidate_variants_fail_closed(changes):
    with pytest.raises((ValueError, TypeError)):
        run(**changes)


def test_total_bar_fold_and_compute_work_limits_are_enforced_before_replay():
    with pytest.raises(ValueError, match="INPUT_BOUND"):
        run(bars(10001))
    with pytest.raises(ValueError, match="FOLDS_BOUND"):
        run(bars(100), test_bars=1)
    with pytest.raises(ValueError, match="WORK_BOUND"):
        run(
            bars(10000),
            train_bars=9000,
            test_bars=1000,
            candidates=[
                {"id": "expensive", "plugin": "donchian", "params": {"ema_slow": 1000}}
            ],
        )


@pytest.mark.parametrize("mutation", ["gap", "duplicate", "reverse", "unused_tail_gap"])
def test_replay_contiguity_contract_also_checks_unused_tail(mutation):
    source = bars(35)
    if mutation == "gap":
        source.pop(3)
    elif mutation == "duplicate":
        source[3] = source[2]
    elif mutation == "reverse":
        source.reverse()
    else:
        source[-1] = replace(source[-1], t=source[-1].t + MINUTE)
    with pytest.raises(ValueError, match="QUANT_REPLAY_GAP"):
        run(source)


@pytest.mark.parametrize(
    "events",
    [
        [{"at_ms": True, "rate": "0", "mark_price": "100"}],
        [{"at_ms": 1, "rate": "0", "mark_price": "100"}],
        [{"at_ms": 0, "rate": 0.001, "mark_price": "100"}],
        [{"at_ms": 0, "rate": "NaN", "mark_price": "100"}],
        [{"at_ms": 0, "rate": "0", "mark_price": "0"}],
        [{"at_ms": 100 * MINUTE, "rate": "0", "mark_price": "100"}],
        [{"at_ms": 0, "rate": "0", "mark_price": "100"}] * 2,
    ],
)
def test_funding_inputs_are_strict(events):
    with pytest.raises(ValueError):
        run(funding=events)
