"""Pure recovery proofs; no PostgreSQL, services or venue required."""

from copy import deepcopy
from uuid import UUID

import pytest

from services.v2_trading_health import retain_unknown_queue, signal_lag_state

LANE = "s6:s3"
CODE = "SIGNAL_CONSUMPTION_LAG"
UNKNOWN = "SIGNAL_QUEUE_OBSERVATION_UNAVAILABLE"


def key(value):
    return str(UUID(int=value))


def entry(*ids, status="COMPLETE", limit=1):
    return {
        "status": status,
        "reason": "BOUNDED_HOT_QUEUE_OBSERVED"
        if status == "COMPLETE"
        else "READ_FAILED",
        "recent_from_ms": 900,
        "limit_per_source": limit,
        "lag_signal_ids": list(ids),
    }


def legacy():
    return {
        "observed_at_ms": 950,
        "active": [CODE],
        "queue_diagnostics": {LANE: {"scope": "OVER_LAG_THRESHOLD_UNRECEIPTED"}},
        "findings": {CODE: {LANE: {"count": 1, "oldest_received_at_ms": 100}}},
    }


def maintenance(cold=None, discovery=None):
    return {
        LANE: {
            "history": {
                "cold_expiry": {"last_sweep_completed_at_ms": cold},
                "discovery": {"last_sweep_completed_at_ms": discovery},
            }
        }
    }


def observe(old, current, *, now=1000, cold=None, discovery=None):
    diagnostics, findings = {LANE: deepcopy(current)}, {}
    unknown = retain_unknown_queue(
        diagnostics, old, findings, now, maintenance(cold, discovery)
    )
    saved = {
        "observed_at_ms": now,
        "queue_diagnostics": diagnostics,
        "findings": findings,
    }
    return unknown, diagnostics[LANE], findings, saved


def test_legacy_inflight_completion_is_baseline_not_recovery():
    unknown, queue, findings, _ = observe(legacy(), entry(), cold=990, discovery=995)
    assert unknown and queue["status"] == "UNKNOWN"
    assert queue["historical_recovery_unproven_since_ms"] == 950
    assert findings[CODE][LANE]["observation_status"] == "UNKNOWN"
    assert queue["historical_recovery_sweeps"]["cold_expiry"] == {
        "baseline_completed_at_ms": 990,
        "first_new_completed_at_ms": None,
        "second_new_completed_at_ms": None,
    }


def test_unavailable_first_legacy_read_cannot_lose_gap_as_recent_window_moves():
    old = legacy()
    old["findings"][CODE][LANE]["oldest_received_at_ms"] = 950
    # The legacy input fits the selected recent window, but this failed read
    # has obtained no identities and therefore has proved no coverage.
    unknown, queue, _, old = observe(old, entry(status="UNKNOWN"))
    assert unknown
    assert queue["lag_signal_ids"] == []
    assert queue["historical_recovery_unproven_since_ms"] == 950
    moved_window = {**entry(), "recent_from_ms": 1100}
    unknown, queue, findings, _ = observe(old, moved_window, now=1200)
    assert unknown
    assert queue["status"] == "UNKNOWN"
    assert queue["reason"] == "HISTORICAL_RECOVERY_NOT_PROVEN"
    assert queue["historical_recovery_unproven_since_ms"] == 950
    assert CODE in findings and UNKNOWN in findings


def test_each_lane_needs_two_distinct_new_completions_after_initial_watermark():
    *_, old = observe(legacy(), entry(), cold=990, discovery=995)
    unknown, _, _, old = observe(old, entry(), now=1100, cold=1050, discovery=1055)
    assert unknown
    unknown, _, _, old = observe(old, entry(), now=1200, cold=1050, discovery=1055)
    assert unknown  # Repeating the same cached completion does not progress.
    unknown, _, _, old = observe(old, entry(), now=1300, cold=1250, discovery=1055)
    assert unknown  # One completed lane cannot recover the other.
    unknown, queue, findings, _ = observe(
        old, entry(), now=1400, cold=1250, discovery=1355
    )
    assert not unknown and queue["status"] == "COMPLETE"
    assert "historical_recovery_unproven_since_ms" not in queue
    assert CODE not in findings and UNKNOWN not in findings


@pytest.mark.parametrize("watermark", [None, -1, True, "1200", 999999])
def test_invalid_or_future_completion_never_proves_recovery(watermark):
    *_, old = observe(legacy(), entry(), cold=990, discovery=995)
    unknown, queue, _, _ = observe(
        old, entry(), now=1300, cold=watermark, discovery=watermark
    )
    assert unknown
    for proof in queue["historical_recovery_sweeps"].values():
        assert proof["first_new_completed_at_ms"] is None
        assert proof["second_new_completed_at_ms"] is None


def test_regressed_watermarks_and_missing_maintenance_preserve_gap():
    *_, old = observe(legacy(), entry(), cold=990, discovery=995)
    *_, old = observe(old, entry(), now=1100, cold=1050, discovery=1055)
    unknown, queue, _, old = observe(old, entry(), now=1200, cold=980, discovery=985)
    assert unknown
    assert (
        queue["historical_recovery_sweeps"]["cold_expiry"]["first_new_completed_at_ms"]
        == 1050
    )
    assert observe(old, entry(), now=1300)[0]


def test_complete_hot_read_prunes_old_ids_even_while_history_gap_is_unknown():
    *_, old = observe(legacy(), entry(key(1), key(2), key(3), key(4)))
    unknown, queue, _, _ = observe(old, entry(key(5), key(6), key(7), key(8)), now=1100)
    assert unknown
    assert queue["lag_signal_ids"] == [key(5), key(6), key(7), key(8)]
    assert "tracking_gap_reason" not in queue


def test_failed_read_retains_known_ids_with_explicit_bounded_overflow_gap():
    old = {"queue_diagnostics": {LANE: entry(key(1), key(2), key(3))}}
    unknown, queue, _, _ = observe(old, entry(key(4), key(5), status="UNKNOWN"))
    assert unknown
    assert queue["lag_signal_ids"] == [key(1), key(2), key(3), key(4)]
    assert queue["tracking_gap_reason"] == "QUEUE_TRACKING_LIMIT_REACHED"
    assert queue["historical_recovery_unproven_since_ms"] == 1000


def test_malformed_keys_create_gap_instead_of_crashing_or_silent_drop():
    old = {"queue_diagnostics": {LANE: {"lag_signal_ids": [key(1), {}, "bad"]}}}
    unknown, queue, _, _ = observe(old, entry(status="UNKNOWN"))
    assert unknown and queue["lag_signal_ids"] == [key(1)]
    assert queue["tracking_gap_reason"] == "QUEUE_TRACKING_KEYS_INVALID"


def test_helper_truncation_flag_creates_gap_even_if_returned_keys_fit_limit():
    current = entry(key(1), key(2), key(3), key(4), status="UNKNOWN")
    current["tracking_overflow"] = True
    unknown, queue, _, _ = observe({}, current)
    assert unknown and len(queue["lag_signal_ids"]) == 4
    assert queue["tracking_gap_reason"] == "QUEUE_TRACKING_LIMIT_REACHED"
    assert queue["historical_recovery_unproven_since_ms"] == 1000


def test_new_overflow_resets_older_recovery_progress():
    *_, old = observe(legacy(), entry(key(1), key(2), key(3)), cold=990, discovery=995)
    *_, old = observe(
        old, entry(key(1), key(2), key(3)), now=1100, cold=1050, discovery=1055
    )
    unknown, queue, _, _ = observe(
        old,
        entry(key(4), key(5), status="UNKNOWN"),
        now=1200,
        cold=1150,
        discovery=1155,
    )
    assert unknown and queue["historical_recovery_unproven_since_ms"] == 1200
    assert queue["historical_recovery_sweeps"]["cold_expiry"] == {
        "baseline_completed_at_ms": 1150,
        "first_new_completed_at_ms": None,
        "second_new_completed_at_ms": None,
    }


def test_complete_read_can_clear_exact_old_lag_without_history_gap():
    old = legacy()
    old["queue_diagnostics"][LANE] = entry(key(1))
    unknown, queue, findings, _ = observe(old, entry())
    assert not unknown and queue["lag_signal_ids"] == []
    assert CODE not in findings and UNKNOWN not in findings


@pytest.mark.parametrize("findings", [{}, {CODE: {LANE: {"count": 1}}}])
def test_unknown_current_lag_is_null_not_false_or_stale_true(findings):
    result = signal_lag_state(
        findings,
        [CODE],
        {CODE: 100},
        {},
        200,
        {"health.signal_recovery_seconds": 60},
        observation_unknown=True,
    )
    assert result["status"] == "UNKNOWN"
    assert result["current_lag_observed"] is None
    assert result["alarm_active"] is True


def test_empty_first_deployment_keeps_explicit_hot_scope_without_inventing_alarm():
    unknown, queue, findings, _ = observe({}, entry())
    assert not unknown and queue["status"] == "COMPLETE" and not findings
