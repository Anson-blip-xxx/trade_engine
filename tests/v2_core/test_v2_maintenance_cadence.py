"""Expiry progresses without rerunning history or hiding dependency failures."""

import threading
from types import SimpleNamespace

import pytest
from psycopg.errors import LockNotAvailable, QueryCanceled

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
        scheduler.last_expiry_metrics = {
            "history_page": {
                "scanned": 0,
                "page_size": 1000,
                "sweep_complete": True,
                "cursor_present": False,
                "elapsed_ms": 0,
                "sweeps": 1,
            }
        }
        return 2

    def discover(**kwargs):
        calls.append(("discovery", now[0], kwargs))
        if failures["discovery"]:
            raise ConnectionError("secret-discovery-details")
        scheduler.last_run_metrics = {
            "history_page": {
                "scanned": 0,
                "page_size": 1000,
                "sweep_complete": True,
                "cursor_present": False,
                "elapsed_ms": 0,
                "sweeps": 1,
            }
        }

    scheduler = SimpleNamespace(
        worker=SimpleNamespace(
            runtime=SimpleNamespace(_now=lambda: 100000 + now[0] * 1000)
        ),
        expire_pending=expire,
        run_once=discover,
        progress=lambda: pytest.fail("background must not count full history"),
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
    now[0] = 4
    maintenance.run_once()
    report = maintenance.snapshots["qa"]
    assert report["status"] == "UNAVAILABLE"
    assert report["component_failures"] == {"discovery": "ConnectionError"}
    assert "secret" not in str(report)
    now[0] = 5
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
    ] == [0, 15, 30, 45]
    assert all(
        options.get("history_page_size") == 1000 and options["budget_ms"] <= 250
        for op, _, options in calls
        if op == "expiry" and not options.get("hot_only")
    )
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


def test_unfinished_history_continues_in_bounded_pages_not_minute_long_scans():
    maintenance, now, calls, _ = fixture()
    scheduler = maintenance.work[0][1]
    expire, discover = scheduler.expire_pending, scheduler.run_once

    def paged_expire(**kwargs):
        result = expire(**kwargs)
        scheduler.last_expiry_metrics["history_page"].update(
            scanned=1000,
            sweep_complete=False,
            cursor_present=True,
        )
        return result

    def paged_discover(**kwargs):
        discover(**kwargs)
        scheduler.last_run_metrics["history_page"].update(
            scanned=1000,
            sweep_complete=False,
            cursor_present=True,
        )

    scheduler.expire_pending, scheduler.run_once = paged_expire, paged_discover
    maintenance.run_once()
    assert len([c for c in calls if c[0] == "discovery"]) == 8
    report = maintenance.snapshots["qa"]
    assert report["history"]["discovery"]["scanned_total"] == 8000
    assert report["progress"]["caught_up"] is None
    assert report["progress"]["mode"] == "BOUNDED_HISTORY_SWEEP"
    now[0] = 0.5
    maintenance.run_once()
    assert len([c for c in calls if c[0] == "discovery"]) == 8
    now[0] = 1
    maintenance.run_once()
    assert len([c for c in calls if c[0] == "discovery"]) == 16
    assert maintenance.snapshots["qa"]["history"]["discovery"]["scanned_total"] == 16000


def test_history_wall_budget_and_failure_do_not_starve_the_other_lane():
    maintenance, now, calls, failures = fixture()
    scheduler = maintenance.work[0][1]
    expire = scheduler.expire_pending

    def slow_expire(**kwargs):
        if not kwargs.get("hot_only"):
            now[0] += 0.3
        return expire(**kwargs)

    scheduler.expire_pending = slow_expire
    failures["cold_expiry"] = True
    maintenance.run_once()
    discovery = [c for c in calls if c[0] == "discovery"]
    assert discovery == []  # The remaining 200ms cannot fund a full operation.
    report = maintenance.snapshots["qa"]
    assert report["component_failures"] == {"cold_expiry": "ValueError"}
    assert maintenance.history_next_lane["qa"] == "discovery"
    before = len(calls)
    now[0] = 5.31
    maintenance.run_once()
    assert calls[before][0] == "discovery"
    discovery = [c for c in calls if c[0] == "discovery"]
    assert len(discovery) == 1 and discovery[0][2]["budget_ms"] == 250
    report = maintenance.snapshots["qa"]
    assert report["history"]["discovery"]["sweep_complete"] is True


def test_snapshot_does_not_share_mutable_nested_history():
    maintenance, _, _, _ = fixture()
    maintenance.run_once()
    report = maintenance.snapshot(None)
    report["history"]["cold_expiry"]["scanned_total"] = 999999
    assert maintenance.snapshots["qa"]["history"]["cold_expiry"]["scanned_total"] == 0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("scheduler.history_page_size", 99),
        ("scheduler.history_page_size", 1001),
        ("scheduler.history_pages_per_tick", 0),
        ("scheduler.history_pages_per_tick", 33),
        ("scheduler.history_budget_ms", 49),
        ("scheduler.history_budget_ms", 2001),
        ("scheduler.history_interval_seconds", 0),
        ("scheduler.history_interval_seconds", 61),
    ],
)
def test_history_resource_limits_are_configurable_and_validated(key, value):
    with pytest.raises(ValueError):
        resolve({key: value})


def failing_history_lane(maintenance, component, failure, *, threshold=0):
    scheduler = maintenance.work[0][1]
    method = "expire_pending" if component == "cold_expiry" else "run_once"
    original = getattr(scheduler, method)
    progress = []

    def bounded(**kwargs):
        result = original(**kwargs)
        if kwargs.get("hot_only"):
            return result
        size = kwargs["history_page_size"]
        if size > threshold:
            raise failure()
        metrics = (
            scheduler.last_expiry_metrics
            if component == "cold_expiry"
            else scheduler.last_run_metrics
        )
        metrics["history_page"].update(page_size=size, scanned=size)
        progress.append(size)
        return result

    setattr(scheduler, method, bounded)
    return progress


def history_calls(calls, component):
    return [
        options
        for name, _, options in calls
        if (
            name == "expiry" and not options.get("hot_only")
            if component == "cold_expiry"
            else name == "discovery"
        )
    ]


@pytest.mark.parametrize("component", ["cold_expiry", "discovery"])
@pytest.mark.parametrize(
    "failure",
    [
        lambda: QueryCanceled("QA query timeout"),
        lambda: LockNotAvailable("QA lock timeout"),
        lambda: TimeoutError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
    ],
)
def test_budget_failures_shrink_independent_pages_to_one_without_widening_budget(
    component, failure
):
    maintenance, now, calls, _ = fixture()
    failing_history_lane(maintenance, component, failure)
    sizes = [1000, 500, 250, 125, 62, 31, 15, 7, 3, 1, 1]
    for index, expected in enumerate(sizes):
        now[0] = index * 5
        before = len(history_calls(calls, component))
        maintenance.run_once()
        attempted = history_calls(calls, component)
        assert len(attempted) == before + 1  # Never retry a failure in this tick.
        assert attempted[-1]["history_page_size"] == expected
        assert attempted[-1]["budget_ms"] == 250
        report = maintenance.snapshots["qa"]
        assert report["status"] == "UNAVAILABLE"
        assert report["history"][component]["effective_page_size"] == max(
            1, expected // 2
        )
    page = maintenance.snapshots["qa"]["history"][component]
    assert page["page_reductions"] == 9
    assert page["minimum_page_reached"] is True
    assert "QA" not in str(maintenance.snapshots["qa"])
    assert all(
        call["history_page_size"] == 1000
        for call in history_calls(
            calls, "discovery" if component == "cold_expiry" else "cold_expiry"
        )
    )


@pytest.mark.parametrize(
    "failure",
    [
        lambda: TimeoutError("unrelated transport timeout"),
        lambda: ValueError("HISTORY_PAGE_BUDGET_EXHAUSTED"),
        lambda: ConnectionError("QA connection failure"),
    ],
)
def test_unrelated_errors_do_not_shrink_or_hide_dependency_failure(failure):
    maintenance, now, calls, _ = fixture()
    failing_history_lane(maintenance, "cold_expiry", failure)
    for current in (0, 5, 10):
        now[0] = current
        maintenance.run_once()
    assert [
        row["history_page_size"] for row in history_calls(calls, "cold_expiry")
    ] == [1000] * 3
    page = maintenance.snapshots["qa"]["history"]["cold_expiry"]
    assert page["effective_page_size"] == 1000
    assert page["page_reductions"] == 0
    assert maintenance.snapshots["qa"]["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("component", ["cold_expiry", "discovery"])
def test_small_successful_page_clears_failure_and_keeps_cap_without_oscillation(
    component,
):
    maintenance, now, calls, _ = fixture()
    progress = failing_history_lane(
        maintenance, component, lambda: QueryCanceled("QA dense page"), threshold=200
    )
    for current in (0, 5, 10, 15):
        now[0] = current
        maintenance.run_once()
    assert [row["history_page_size"] for row in history_calls(calls, component)] == [
        1000,
        500,
        250,
        125,
    ]
    report = maintenance.snapshots["qa"]
    assert report["status"] == "CURRENT"
    assert report["component_failures"] == {}
    assert report["history"][component]["effective_page_size"] == 125
    assert report["history"][component]["page_reductions"] == 3
    assert report["history"][component]["page_size"] == 125
    assert progress == [125]
    now[0] = 75
    maintenance.run_once()
    assert history_calls(calls, component)[-1]["history_page_size"] == 125
    assert progress == [125, 125]


def test_adaptive_page_caps_are_separate_for_each_consumer_and_lane():
    maintenance, now, calls, _ = fixture()
    second, _, second_calls, _ = fixture()
    first_scheduler = maintenance.work[0][1]
    second_scheduler = second.work[0][1]
    first_scheduler.identity = "qa"
    second_scheduler.identity = "qa-other"
    maintenance.key = lambda scheduler: scheduler.identity
    maintenance.work.append(("qa-other", second_scheduler))
    failing_history_lane(
        maintenance, "cold_expiry", lambda: QueryCanceled("QA timeout")
    )
    for current in (0, 5):
        now[0] = current
        maintenance.run_once()
    assert [
        row["history_page_size"] for row in history_calls(calls, "cold_expiry")
    ] == [1000, 500]
    assert all(
        row["history_page_size"] == 1000 for row in history_calls(calls, "discovery")
    )
    assert all(
        row["history_page_size"] == 1000
        for row in history_calls(second_calls, "cold_expiry")
    )
    assert all(
        row["history_page_size"] == 1000
        for row in history_calls(second_calls, "discovery")
    )
    assert maintenance.snapshots["qa-other"]["status"] == "CURRENT"
    assert (
        maintenance.snapshots["qa-other"]["history"]["cold_expiry"]["page_reductions"]
        == 0
    )


def test_configured_lower_ceiling_clamps_existing_caps_and_success_does_not_raise_them():
    maintenance, now, calls, _ = fixture()
    configured = [1000]
    maintenance.settings = lambda: resolve(
        {"scheduler.history_page_size": configured[0]}
    )
    maintenance.run_once()
    assert history_calls(calls, "cold_expiry")[-1]["history_page_size"] == 1000
    configured[0] = 100
    now[0] = 60
    maintenance.run_once()
    assert history_calls(calls, "cold_expiry")[-1]["history_page_size"] == 100
    assert history_calls(calls, "discovery")[-1]["history_page_size"] == 100
    configured[0] = 1000
    now[0] = 120
    maintenance.run_once()
    assert history_calls(calls, "cold_expiry")[-1]["history_page_size"] == 100
    assert maintenance.snapshots["qa"]["history"]["cold_expiry"]["page_reductions"] == 0


def test_adaptive_cap_retries_only_on_scheduled_tick_and_does_not_change_hot_expiry():
    maintenance, now, calls, _ = fixture()
    failing_history_lane(
        maintenance, "cold_expiry", lambda: QueryCanceled("QA timeout")
    )
    for current in (0, 1, 2, 4, 5, 15):
        now[0] = current
        maintenance.run_once()
    assert [
        at
        for name, at, options in calls
        if name == "expiry" and not options.get("hot_only")
    ] == [0, 5, 15]
    hot = [
        options
        for name, _, options in calls
        if name == "expiry" and options.get("hot_only")
    ]
    assert len(hot) == 2
    assert all(
        "history_page_size" not in options and options["budget_ms"] == 2000
        for options in hot
    )


def test_normal_pages_never_receive_a_tail_fragment_or_spuriously_shrink():
    maintenance, now, calls, _ = fixture()
    scheduler = maintenance.work[0][1]
    expire, discover = scheduler.expire_pending, scheduler.run_once

    def normal_expiry(**kwargs):
        result = expire(**kwargs)
        if not kwargs.get("hot_only"):
            now[0] += 0.1
            scheduler.last_expiry_metrics["history_page"].update(
                scanned=1000, sweep_complete=False, cursor_present=True
            )
        return result

    def normal_discovery(**kwargs):
        result = discover(**kwargs)
        now[0] += 0.1
        scheduler.last_run_metrics["history_page"].update(
            scanned=1000, sweep_complete=False, cursor_present=True
        )
        return result

    scheduler.expire_pending, scheduler.run_once = normal_expiry, normal_discovery
    maintenance.run_once()
    operations = [options for _, _, options in calls if "history_page_size" in options]
    assert len(operations) == 3  # 200ms remaining: wait, don't start a 200ms probe.
    assert all(row["budget_ms"] == 250 for row in operations)
    assert all(row["history_page_size"] == 1000 for row in operations)
    assert maintenance.snapshots["qa"]["component_failures"] == {}
    assert maintenance.history_page_reductions == {}
    assert maintenance.history_next_lane["qa"] == "discovery"
    before = len(calls)
    now[0] = 1.31
    maintenance.run_once()
    assert calls[before][0] == "discovery"
    assert maintenance.history_page_reductions == {}
    assert all(cap == 1000 for cap in maintenance.history_page_caps.values())


def test_fifty_millisecond_tail_does_not_launch_fail_or_reduce_a_page():
    maintenance, _, calls, _ = fixture()
    scheduler = maintenance.work[0][1]
    # Model scheduling/connection overhead between computing this tick's
    # deadline and attempting an operation; only 50ms now remain.
    readings = iter((0.0, 0.45))
    maintenance.monotonic = lambda: next(readings)
    report, failures = {}, {}
    assert maintenance._history(scheduler, report, failures, resolve()) is False
    assert calls == []
    assert failures == {}
    assert report == {}
    assert maintenance.history_page_caps == {}
    assert maintenance.history_page_reductions == {}
    assert maintenance.history_next_lane["qa"] == "cold_expiry"


def test_slow_successful_expiry_yields_full_budget_to_discovery_next_tick():
    maintenance, now, calls, _ = fixture()
    scheduler = maintenance.work[0][1]
    expire, discover = scheduler.expire_pending, scheduler.run_once

    def slow_expiry(**kwargs):
        result = expire(**kwargs)
        if not kwargs.get("hot_only"):
            # Includes connection/commit overhead outside statement deadlines.
            now[0] += 0.3
            scheduler.last_expiry_metrics["history_page"]["sweep_complete"] = False
        return result

    def unfinished_discovery(**kwargs):
        result = discover(**kwargs)
        scheduler.last_run_metrics["history_page"]["sweep_complete"] = False
        return result

    scheduler.expire_pending, scheduler.run_once = slow_expiry, unfinished_discovery
    maintenance.run_once()
    assert history_calls(calls, "discovery") == []
    assert maintenance.history_next_lane["qa"] == "discovery"
    assert maintenance.snapshots["qa"]["status"] == "CURRENT"
    before = len(calls)
    now[0] = 1.31
    maintenance.run_once()
    assert calls[before][0] == "discovery"
    assert calls[before][2]["budget_ms"] == 250
    assert maintenance.history_page_reductions == {}
    assert all(
        row["budget_ms"] == 250
        for component in ("cold_expiry", "discovery")
        for row in history_calls(calls, component)
    )
