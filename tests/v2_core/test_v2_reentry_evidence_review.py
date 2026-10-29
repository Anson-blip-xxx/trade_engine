"""Pure audit-reducer QA; no database, model or venue clients."""

import runpy
from copy import deepcopy
from pathlib import Path

import pytest

MODULE = runpy.run_path(
    str(
        Path(__file__).resolve().parents[2]
        / "scripts/research_v2_reentry_evidence_readonly.py"
    )
)
summarize = MODULE["summarize"]
timing_audit = MODULE["timing_audit"]


def evidence():
    return {
        "decided_ms": 180000,
        "observed_ms": 181000,
        "intent_created_ms": 180200,
        "payload": {
            "candidate": {"decided_ms": 180000},
            "previous": {"exit_known_ms": 120000},
            "bars": [{"t": 120000}],
            "policy": {"timeframe_minutes": 1},
            "assessment": {"status": "WAIT"},
            "inputs_digest": "digest",
            "collection_completed_ms": 181500,
            "archive_arrival_at_decision_proven": False,
        },
    }


def audit(rows):
    return timing_audit(
        rows,
        recompute=lambda payload: payload["assessment"],
        digest=lambda value: "digest",
    )


def test_open_or_unknown_outcomes_are_not_zero_return_samples():
    result = summarize(
        [{"id": "x", "fees_usdt": None}],
        {"x": {"state": "OPEN_OR_PARTIAL_EXIT", "net_pnl": None}},
    )
    assert result["settled"] == 0
    assert result["actual_net_pnl_usdt"] is None
    assert result["known_recorded_fees_usdt"] is None


def test_net_is_not_charged_fees_twice_and_unknown_fees_stay_unknown():
    result = summarize(
        [{"id": "x", "fees_usdt": "0.2"}, {"id": "y", "fees_usdt": None}],
        {
            "x": {"state": "SETTLED", "net_pnl": "1.1"},
            "y": {"state": "SETTLED", "net_pnl": "-0.4"},
        },
    )
    assert result["actual_net_pnl_usdt"] == "0.7"
    assert result["known_recorded_fees_usdt"] == "0.2"
    assert result["fees_known_settlements"] == 1
    assert result["settled"] == 2


def test_closed_event_time_does_not_prove_predecision_availability():
    result = audit([evidence()])
    assert result["future_or_unclosed_bar"] == 0
    assert result["completed_after_decision"] == 1
    assert result["completed_before_or_at_decision"] == 0
    assert result["archive_arrival_at_decision_proven"] == 0


def test_future_bar_and_late_prior_close_are_counted():
    row = evidence()
    row["payload"]["bars"][0]["t"] = 180000
    row["payload"]["previous"]["exit_known_ms"] = 180001
    result = audit([row])
    assert result["future_or_unclosed_bar"] == 1
    assert result["prior_exit_known_after_decision"] == 1


def test_missing_completion_is_unknown_not_false_predecision_proof():
    row = evidence()
    del row["payload"]["collection_completed_ms"]
    result = audit([row])
    assert result["completion_timestamp_missing"] == 1
    assert result.get("completed_before_or_at_decision", 0) == 0


def test_changed_inputs_and_changed_replay_result_are_not_silent():
    row = evidence()
    before = deepcopy(row)
    result = timing_audit(
        [row],
        recompute=lambda payload: {"status": "READY"},
        digest=lambda value: "changed",
    )
    assert result["input_digest_mismatch"] == 1
    assert result["recomputed_assessment_mismatch"] == 1
    assert row == before


def test_invalid_replay_is_recorded_not_promoted():
    def fail(payload):
        raise ValueError("invalid")

    result = timing_audit([evidence()], recompute=fail, digest=lambda value: "digest")
    assert result["recomputed_assessment_invalid"] == 1


@pytest.mark.parametrize(
    "value", [None, 0, True, "NaN", "Infinity", "-Infinity", "1e999", "invalid"]
)
def test_invalid_settled_net_is_rejected_not_zeroed(value):
    with pytest.raises(ValueError, match="SETTLEMENT_AMOUNT_INVALID"):
        summarize(
            [{"id": "x", "fees_usdt": None}],
            {"x": {"state": "SETTLED", "net_pnl": value}},
        )


@pytest.mark.parametrize("value", [False, "NaN", "Infinity", "invalid"])
def test_present_but_invalid_fee_is_rejected(value):
    with pytest.raises(ValueError, match="SETTLEMENT_AMOUNT_INVALID"):
        summarize(
            [{"id": "x", "fees_usdt": value}],
            {"x": {"state": "SETTLED", "net_pnl": "1"}},
        )
