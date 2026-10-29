"""Expiry progresses without rerunning history or hiding dependency failures."""

import threading
from types import SimpleNamespace

from services.v2_signal_maintenance import SignalMaintenance
from v2_core.runtime_policy import resolve


def fixture():
    now, calls = [0], []
    failures = {"expiry": False, "cold_expiry": False, "discovery": False}

    def expire(**kwargs):
        calls.append(("expiry", now[0], kwargs))
        if failures["expiry"] or (
            failures["cold_expiry"] and not kwargs.get("hot_only")
        ):
            raise ValueError("secret-expiry-details")
        return 2

    def discover(**kwargs):
        calls.append(("discovery", now[0], kwargs))
        if failures["discovery"]:
            raise ConnectionError("secret-discovery-details")

    scheduler = SimpleNamespace(
        worker=SimpleNamespace(
            runtime=SimpleNamespace(_now=lambda: 100000 + now[0] * 1000)
        ),
        expire_pending=expire,
        run_once=discover,
        progress=lambda: {"caught_up": True},
    )
    maintenance = SignalMaintenance(
        [], settings=resolve, stop=threading.Event(), monotonic=lambda: now[0]
    )
    maintenance.key = lambda _: "qa"
    maintenance.work = [("qa", scheduler)]
    return maintenance, now, calls, failures


def test_expiry_runs_before_cold_reconciliation_and_with_bounded_sql():
    maintenance, now, calls, _ = fixture()
    for value in (0, 14, 15, 30, 45, 60):
        now[0] = value
        maintenance.run_once()
    assert [
        at for op, at, options in calls if op == "expiry" and options.get("hot_only")
    ] == [0, 15, 30, 45, 60]
    assert [
        at
        for op, at, options in calls
        if op == "expiry" and not options.get("hot_only")
    ] == [0, 60]
    assert [at for op, at, _ in calls if op == "discovery"] == [0, 60]
    assert all(
        options["reconcile_seconds"] == 60 and options["budget_ms"] == 2000
        for op, _, options in calls
        if op == "expiry" and options.get("hot_only")
    )


def test_successful_expiry_does_not_clear_unrecovered_discovery_failure():
    maintenance, now, _, failures = fixture()
    failures["discovery"] = True
    maintenance.run_once()
    failures["discovery"] = False
    now[0] = 15
    maintenance.run_once()
    report = maintenance.snapshots["qa"]
    assert report["status"] == "UNAVAILABLE"
    assert report["component_failures"] == {"discovery": "ConnectionError"}
    assert "secret" not in str(report)
    now[0] = 60
    maintenance.run_once()
    assert maintenance.snapshots["qa"]["status"] == "CURRENT"


def test_expiry_failure_recovers_without_waiting_for_full_reconciliation():
    maintenance, now, calls, failures = fixture()
    maintenance.run_once()
    failures["expiry"] = True
    now[0] = 15
    maintenance.run_once()
    assert maintenance.snapshots["qa"]["status"] == "UNAVAILABLE"
    failures["expiry"] = False
    now[0] = 30
    maintenance.run_once()
    assert maintenance.snapshots["qa"]["status"] == "CURRENT"
    assert [at for op, at, _ in calls if op == "discovery"] == [0]


def test_cold_failure_never_turns_fast_expiry_into_repeated_history_scans():
    maintenance, now, calls, failures = fixture()
    failures["cold_expiry"] = True
    for value in (0, 15, 30, 45):
        now[0] = value
        maintenance.run_once()
    assert [
        at
        for op, at, options in calls
        if op == "expiry" and not options.get("hot_only")
    ] == [0]
    assert maintenance.snapshots["qa"]["component_failures"] == {
        "cold_expiry": "ValueError"
    }
    assert maintenance.snapshots["qa"]["status"] == "UNAVAILABLE"
    assert maintenance.snapshots["qa"]["expiry_observed_at_ms"] == 145000
    failures["cold_expiry"] = False
    now[0] = 60
    maintenance.run_once()
    assert maintenance.snapshots["qa"]["status"] == "CURRENT"


def test_stopped_maintenance_never_cleans_or_discovers():
    maintenance, _, calls, _ = fixture()
    maintenance.stop.set()
    maintenance.run_once()
    assert calls == []
