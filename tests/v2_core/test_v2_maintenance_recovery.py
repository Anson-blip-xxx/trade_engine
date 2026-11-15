"""Capacity recovery is bounded and does not confuse dependency stalls with load."""

import json

import pytest
from psycopg.errors import LockNotAvailable, QueryCanceled
from test_v2_maintenance_cadence import fixture, history_calls

from services.v2_signal_maintenance import SignalMaintenance
from v2_core.runtime_policy import resolve


def lane(maintenance, component="cold_expiry"):
    scheduler = maintenance.work[0][1]
    method = "expire_pending" if component == "cold_expiry" else "run_once"
    original = getattr(scheduler, method)
    state = {
        "error": None,
        "phase": "PAGE_READ",
        "elapsed_ms": 20,
        "setup_elapsed_ms": 0,
    }

    def run(**kwargs):
        result = original(**kwargs)
        if kwargs.get("hot_only"):
            return result
        size = kwargs["history_page_size"]
        scheduler.last_history_diagnostics = {
            "component": component,
            "phase": state["phase"] if state["error"] else "COMPLETE",
            "status": "FAILED" if state["error"] else "COMPLETE",
            "budget_ms": kwargs["budget_ms"],
            "page_size": size,
            "scanned": state.get("scanned", size),
            "elapsed_ms": state["elapsed_ms"],
            "setup_elapsed_ms": state["setup_elapsed_ms"],
            "cursor_advanced": not bool(state["error"]),
            "commit_confirmed": not bool(state["error"]),
            "secret": "must-not-leak",
        }
        if state["error"]:
            raise state["error"]
        metrics = (
            scheduler.last_expiry_metrics
            if component == "cold_expiry"
            else scheduler.last_run_metrics
        )
        metrics["history_page"].update(
            page_size=size, scanned=state.get("scanned", size)
        )
        return result

    setattr(scheduler, method, run)
    return state


def tick(maintenance):
    report, failures = {}, {}
    maintenance._history(
        maintenance.work[0][1], report, failures, maintenance.settings()
    )
    return report, failures


@pytest.mark.parametrize(
    "phase,error,setup,kind,reduced",
    [
        (
            "CONNECT_SETUP",
            TimeoutError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
            0,
            "CONNECT_SETUP_BUDGET",
            False,
        ),
        (
            "CONNECT_SETUP",
            QueryCanceled("private SQL"),
            0,
            "CONNECT_SETUP_BUDGET",
            False,
        ),
        ("PAGE_READ", LockNotAvailable("private SQL"), 0, "LOCK_CONTENTION", False),
        ("RECEIPT_WRITE", LockNotAvailable("private SQL"), 0, "LOCK_CONTENTION", False),
        ("COMMIT", QueryCanceled("private SQL"), 0, "COMMIT_TIMEOUT", False),
        (
            "PAGE_READ",
            TimeoutError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
            200,
            "SETUP_PRESSURE_OR_UNKNOWN",
            False,
        ),
        (
            "PAGE_READ",
            TimeoutError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
            0,
            "PAGE_CAPACITY_TIMEOUT",
            True,
        ),
        ("ELIGIBILITY", QueryCanceled("private SQL"), 0, "PAGE_CAPACITY_TIMEOUT", True),
        (
            "RECEIPT_WRITE",
            QueryCanceled("private SQL"),
            0,
            "PAGE_CAPACITY_TIMEOUT",
            True,
        ),
        ("TASK_WRITE", QueryCanceled("private SQL"), 0, "PAGE_CAPACITY_TIMEOUT", True),
        ("PAGE_READ", TimeoutError("network timeout"), 0, "DEPENDENCY_FAILURE", False),
        (
            "PAGE_READ",
            ValueError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
            0,
            "DEPENDENCY_FAILURE",
            False,
        ),
    ],
)
def test_failure_phase_controls_reduction_without_exposing_sql(
    phase, error, setup, kind, reduced
):
    maintenance, _, calls, _ = fixture()
    state = lane(maintenance)
    state.update(error=error, phase=phase, setup_elapsed_ms=setup)
    report, failures = tick(maintenance)
    page = report["history"]["cold_expiry"]
    assert failures == {"cold_expiry": type(error).__name__}
    assert page["effective_page_size"] == (500 if reduced else 1000)
    assert page["last_attempt"]["failure_kind"] == kind
    assert len(history_calls(calls, "cold_expiry")) == 1
    assert len(history_calls(calls, "discovery")) == 1
    assert "private SQL" not in json.dumps(report)
    assert "must-not-leak" not in json.dumps(report)


def test_missing_current_phase_never_reuses_stale_capacity_failure():
    maintenance, _, _, _ = fixture()
    scheduler = maintenance.work[0][1]
    scheduler.last_history_diagnostics = {
        "component": "cold_expiry",
        "status": "FAILED",
        "phase": "PAGE_READ",
    }
    scheduler.expire_pending = lambda **_: (_ for _ in ()).throw(
        QueryCanceled("secret")
    )
    report, failures = tick(maintenance)
    assert failures == {"cold_expiry": "QueryCanceled"}
    page = report["history"]["cold_expiry"]
    assert page["effective_page_size"] == 1000
    assert page["last_attempt"] == {
        "phase": "UNKNOWN",
        "failure_kind": "UNCLASSIFIED_TIMEOUT",
    }


@pytest.mark.parametrize("component", ["cold_expiry", "discovery"])
def test_minimum_page_recovers_after_healthy_full_pages_and_cooldown(component):
    maintenance, now, calls, _ = fixture()
    lane(maintenance, component)
    identity = "qa", component
    maintenance.history_page_caps[identity] = 1
    for _ in range(8):
        tick(maintenance)
    assert maintenance.history_page_caps[identity] == 1  # No immediate oscillation.
    now[0] = 30
    report, _ = tick(maintenance)
    assert maintenance.history_page_caps[identity] == 2
    assert report["history"][component]["recovery_probe"] is True
    assert report["history"][component]["page_recoveries"] == 1
    assert all(row["budget_ms"] == 250 for row in history_calls(calls, component))
    for _ in range(8):
        tick(maintenance)
    assert maintenance.history_page_caps[identity] == 2
    now[0] = 60
    tick(maintenance)
    assert maintenance.history_page_caps[identity] == 3


@pytest.mark.parametrize(
    "changes", [{"elapsed_ms": 150}, {"scanned": 0}, {"scanned": 1}]
)
def test_slow_empty_and_partial_pages_do_not_prove_capacity(changes):
    maintenance, now, _, _ = fixture()
    state = lane(maintenance)
    state.update(changes)
    maintenance.history_page_caps["qa", "cold_expiry"] = 100
    for number in range(12):
        now[0] = number * 60
        tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 100
    assert maintenance.history_page_recovery["qa", "cold_expiry"]["healthy_pages"] == 0


def test_failed_probe_restores_previous_stable_capacity_then_backoff_still_works():
    maintenance, now, calls, _ = fixture()
    state = lane(maintenance)
    maintenance.history_page_caps["qa", "cold_expiry"] = 100
    for number in range(8):
        now[0] = number * 5
        tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 125
    state["error"] = QueryCanceled("private")
    now[0] = 40
    report, _ = tick(maintenance)
    assert history_calls(calls, "cold_expiry")[-1]["history_page_size"] == 125
    assert report["history"]["cold_expiry"]["effective_page_size"] == 100
    assert report["history"]["cold_expiry"]["recovery_probe"] is False
    now[0] = 45
    tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 50


def test_recovery_respects_dynamic_ceiling_and_other_lane():
    maintenance, now, _, _ = fixture()
    lane(maintenance)
    configured = [110]
    maintenance.settings = lambda: resolve(
        {"scheduler.history_page_size": configured[0]}
    )
    maintenance.history_page_caps["qa", "cold_expiry"] = 100
    for number in range(8):
        now[0] = number * 5
        tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 110
    configured[0] = 100
    tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 100
    configured[0] = 1000
    tick(maintenance)
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 100
    assert maintenance.history_page_caps["qa", "discovery"] == 100


def test_dependency_failure_resets_healthy_streak_without_changing_capacity():
    maintenance, now, _, _ = fixture()
    state = lane(maintenance)
    maintenance.history_page_caps["qa", "cold_expiry"] = 100
    for number in range(7):
        now[0] = number * 5
        tick(maintenance)
    state["error"] = LockNotAvailable("private")
    tick(maintenance)
    state["error"] = None
    now[0] = 100
    report, _ = tick(maintenance)
    assert report["history"]["cold_expiry"]["healthy_pages"] == 1
    assert maintenance.history_page_caps["qa", "cold_expiry"] == 100


def test_unknown_diagnostic_fields_are_not_published():
    maintenance, _, _, _ = fixture()
    scheduler = maintenance.work[0][1]
    scheduler.last_history_diagnostics = {
        "component": "cold_expiry",
        "phase": "SECRET_SQL",
        "status": "secret",
        "elapsed_ms": "secret",
        "cursor_advanced": "secret",
        "dsn": "secret",
    }
    assert SignalMaintenance._attempt(scheduler, "cold_expiry") == {"phase": "UNKNOWN"}
