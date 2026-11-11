from copy import deepcopy
from decimal import Decimal

import pytest

from services.v2_trend_entry_review import (
    DAY,
    MINUTE,
    STEP,
    collect_review_candles,
    make_frame_evaluator,
    review_trend_entries,
    summarize_review,
)

START = 3 * DAY


def candles(end=START + 2 * STEP):
    return [
        {
            "t": t, "o": "100", "h": "101", "l": "99", "c": "100",
            "v": "1", "tbv": ".5",
        }
        for t in range(START - DAY, end, MINUTE)
    ]


def evaluator(batch, *, previous, regime, policy):
    rows = batch["candles"]["REDUSDT"]
    assert len(rows) == 1440
    assert rows[0]["t"] == batch["closed_at"] - MINUTE
    assert rows[-1]["t"] == batch["closed_at"] - DAY
    count = previous.get("count", 0) + 1
    return {
        "state": {"count": count},
        "sides": {
            "BUY": {
                "event_present": True,
                "eligible": Decimal(rows[0]["c"]) > 100,
                "reason": "CANDIDATE" if Decimal(rows[0]["c"]) > 100 else "NO_EDGE",
                "sequence": count,
            },
            "SELL": {
                "event_present": False, "eligible": False, "reason": "NO_TREND_EVENT",
            },
        },
    }


def review(rows, **changes):
    args = {
        "start_ms": START, "end_ms": START + 2 * STEP,
        "baseline_evaluator": evaluator, "candidate_evaluator": evaluator,
        "cohort_basis": "DAY_END_SELECTED_DIAGNOSTIC_NOT_OUT_OF_SAMPLE",
        "regime_scenario": "neutral",
    }
    return review_trend_entries({"REDUSDT": rows}, **{**args, **changes})


def test_future_mutation_cannot_change_preceding_decisions_or_input_digests():
    original = candles()
    changed = deepcopy(original)
    for row in changed:
        if row["t"] >= START + STEP:
            row.update(o="200", h="201", l="199", c="200")
    before, after = review(original), review(changed)
    assert before["decisions"][:2] == after["decisions"][:2]
    assert before["decisions"][2] != after["decisions"][2]
    assert after["summaries"][0]["variants"]["CANDIDATE"]["BUY"]["first_eligible_ms"] == START + 2 * STEP


def test_unclosed_current_minute_is_never_given_to_either_evaluator():
    closed = candles(START)
    unclosed = {**closed[-1], "t": START, "o": "999", "h": "999", "l": "999", "c": "999"}
    assert review(closed, end_ms=START) == review(closed + [unclosed], end_ms=START)


def test_previous_state_is_chronological_separate_and_report_is_not_profit():
    source = candles()
    frozen = deepcopy(source)
    result = review(source)
    assert source == frozen
    for variant in ("BASELINE", "CANDIDATE"):
        assert [row["variants"][variant]["BUY"]["sequence"] for row in result["decisions"]] == [1, 2, 3]
    assert result["execution_authorized"] is result["profitability_validated"] is False
    assert result["out_of_sample_validated"] is False
    assert result["alternate_orders_or_pnl"] is None
    assert result["historical_availability_at_entry"] == "NOT_VERIFIED"
    assert result["full_account_or_jev_admission"] == "NOT_RUN"


def test_missing_history_is_unknown_without_evaluation_and_recovery_resets_state():
    rows = [row for row in candles() if row["t"] != START - DAY + STEP]
    result = review(rows)
    for row in result["decisions"][:2]:
        assert row["input_digest"] is None
        assert row["variants"]["CANDIDATE"]["SELL"]["eligible"] is None
        assert row["variants"]["BASELINE"]["BUY"]["reason"] == "INCOMPLETE_CLOSED_HISTORY"
    assert result["decisions"][2]["variants"]["BASELINE"]["BUY"]["sequence"] == 1


def test_evaluator_cannot_mutate_other_variant_or_caller_policy():
    def mutating(batch, *, previous, regime, policy):
        result = evaluator(batch, previous=previous, regime=regime, policy=policy)
        batch["candles"]["REDUSDT"][0]["c"] = "999"
        policy["test"] = "changed"
        previous["count"] = 999
        return result

    rows = candles()
    policy = {"test": "original"}
    result = review(rows, baseline_evaluator=mutating, policy=policy)
    assert all(row["variants"]["CANDIDATE"]["BUY"]["eligible"] is False for row in result["decisions"])
    assert policy == {"test": "original"}
    assert all(row["c"] == "100" for row in rows)


def test_absent_event_cannot_be_counted_as_an_eligible_entry():
    def invalid(batch, **kwargs):
        result = evaluator(batch, **kwargs)
        result["sides"]["SELL"]["eligible"] = True
        return result

    result = review(candles(), candidate_evaluator=invalid)
    assert all(row["variants"]["CANDIDATE"]["SELL"]["status"] == "UNKNOWN" for row in result["decisions"])
    assert result["summaries"][0]["variants"]["CANDIDATE"]["SELL"]["eligible_count"] == 0


@pytest.mark.parametrize("changes", [
    {"start_ms": START + MINUTE}, {"end_ms": START + 3 * DAY},
    {"regime_scenario": "guessed"}, {"cohort_basis": ""},
])
def test_replay_bounds_are_explicit(changes):
    with pytest.raises(ValueError):
        review(candles(), **changes)


def test_archive_reader_pages_and_request_budget_are_finite():
    calls = []

    def read(symbol, start, end):
        calls.append((symbol, start, end))
        return [row for row in reversed(candles()) if start <= row["t"] < end]

    result = collect_review_candles(read, ["REDUSDT"], start_ms=START, end_ms=START + 2 * STEP)
    assert result["errors"] == {}
    assert result["archive_calls"] == 3
    assert result["requested_minutes"] == 1470
    assert all(end - start <= 720 * MINUTE for _, start, end in calls)
    assert len(result["candles"]["REDUSDT"]) == 1470


def test_previous_day_and_diagnostic_day_remain_separate_timestamps():
    result = review(candles(START + DAY), end_ms=START + DAY)
    assert len(result["decisions"]) == 97
    assert result["decisions"][0]["closed_at_ms"] == START
    assert result["decisions"][-1]["closed_at_ms"] == START + DAY
    assert result["out_of_sample_validated"] is False
    compact = summarize_review(result)
    assert "decisions" not in compact
    assert len(compact["daily_summaries"]) == 8
    assert {row["observations"] for row in compact["daily_summaries"]} == {64, 33}
    assert all(row["eligible_count"] == 0 for row in compact["daily_summaries"])
    assert compact["decision_digest"] == result["decision_digest"]


def test_real_versions_compare_same_closed_history_and_keep_future_out():
    from services.v2_s3_candles import PRICE_VERSION, STRUCTURE_VERSION

    rows = candles(START + STEP)
    for index, row in enumerate(rows):
        close = Decimal(50) - Decimal(index) / 100
        row.update(
            o=str(close + Decimal(".009")), h=str(close + Decimal(".02")),
            l=str(close - Decimal(".02")), c=str(close), tbv=".4",
        )
    args = {
        "end_ms": START,
        "baseline_evaluator": make_frame_evaluator(feature_version=PRICE_VERSION, trend_enabled=False),
        "candidate_evaluator": make_frame_evaluator(feature_version=STRUCTURE_VERSION, trend_enabled=True),
    }
    # Supply only the requested point's closed history and its current unclosed
    # minute; later closed observations belong to a subsequent replay window.
    source = rows[:1441]
    before = review(source, **args)
    mutated = deepcopy(source)
    mutated[-1].update(o="500", h="501", l="499", c="500")
    after = review(mutated, **args)
    assert before == after
    point = before["decisions"][0]["variants"]
    assert point["BASELINE"]["SELL"]["event_present"] is True
    assert point["BASELINE"]["SELL"]["reason"] == "strength_below_minimum"
    assert point["CANDIDATE"]["SELL"]["eligible"] is True
    assert point["CANDIDATE"]["SELL"]["proof"]["reward_estimate_kind"] == "ATR_TARGET_NOT_FORECAST"
    assert Decimal(point["CANDIDATE"]["SELL"]["proof"]["planned_net_reward_risk"]) >= 1
