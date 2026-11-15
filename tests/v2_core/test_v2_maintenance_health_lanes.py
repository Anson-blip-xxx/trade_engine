"""An aggregate warning cannot borrow uninterrupted failure from another lane."""

import json
from contextlib import contextmanager
from dataclasses import asdict

import pytest

from services.v2_trading_health import TradingHealth, maintenance_health_projection
from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState, StateKey

database = database_fixture
CODE = "SIGNAL_MAINTENANCE_DEGRADED"


def bad(lane="cold_expiry", committed=None):
    return {
        "status": "UNAVAILABLE",
        "failure_type": "TimeoutError",
        "component_failures": {lane: "TimeoutError"},
        "history": {lane: {"observed_at_ms": committed}},
    }


def project(value, old=None, now=1000000, observed=None):
    return maintenance_health_projection(
        {"s6:s3": value}, old or {}, now, now if observed is None else observed
    )


def test_pure_rotating_consumers_do_not_inherit_aggregate_failure_time():
    previous = {}
    for now, consumer in (
        (1000000, "s6:s3"),
        (1060000, "s8:s3"),
        (1121000, "s6:tv_bridge"),
    ):
        snapshot = {
            name: bad() if name == consumer else {"status": "CURRENT"}
            for name in ("s6:s3", "s8:s3", "s6:tv_bridge")
        }
        finding, previous, first = maintenance_health_projection(
            snapshot, previous, now, now
        )
        assert first == now
        assert finding["consumers"] == [consumer]
    assert now - first < 120000


def test_pure_same_lane_commit_between_failed_snapshots_breaks_fault_continuity():
    _, old, _ = project(bad(committed=999000))
    finding, new, first = project(bad(committed=1080000), old, now=1121000)
    assert first == 1121000
    assert finding["lanes"]["s6:s3"]["cold_expiry"]["status"] == "FAILING"
    assert new["s6:s3"]["cold_expiry"]["last_committed_at_ms"] == 1080000


def test_pure_late_old_commit_does_not_break_a_newer_fault():
    _, old, _ = project(bad())
    _, _, first = project(bad(committed=999999), old, now=1121000)
    assert first == 1000000


@pytest.mark.parametrize("lane", ["cold_expiry", "discovery", "expiry"])
def test_pure_other_lane_progress_does_not_reset_continuous_fault(lane):
    _, old, _ = project(bad(lane))
    value = bad(lane)
    value.update(progress_observed_at_ms=1120000)
    other = "discovery" if lane != "discovery" else "cold_expiry"
    value["history"][other] = {"observed_at_ms": 1120000}
    _, _, first = project(value, old, now=1121000)
    assert first == 1000000


def test_pure_hot_expiry_requires_its_own_commit_evidence():
    _, old, _ = project(bad("expiry"))
    value = bad("expiry")
    value["expiry_observed_at_ms"] = 1080000
    _, _, first = project(value, old, now=1121000)
    assert first == 1121000


def test_pure_rotating_lanes_need_proven_recovery_not_merely_missing_error_key():
    _, old, _ = project(bad("cold_expiry"))
    value = bad("discovery")
    _, unproven, first = project(value, old, now=1060000)
    assert first == 1000000
    assert unproven["s6:s3"]["cold_expiry"]["status"] == "RECOVERY_UNPROVEN"
    value["history"]["cold_expiry"] = {"observed_at_ms": 1050000}
    _, proven, first = project(value, old, now=1060000)
    assert list(proven["s6:s3"]) == ["discovery"]
    assert first == 1060000


def test_pure_worker_failure_is_not_cleared_by_cached_lane_progress():
    value = {"status": "UNAVAILABLE", "failure_type": "ConnectionError"}
    _, old, _ = project(value)
    value["history"] = {"cold_expiry": {"observed_at_ms": 1120000}}
    value["expiry_observed_at_ms"] = 1120000
    _, new, first = project(value, old, now=1121000)
    assert first == 1000000
    assert list(new["s6:s3"]) == ["worker"]


def test_pure_thread_failure_overlay_retains_independent_worker_fault():
    value = bad()
    value["failure_type"] = "ConnectionError"
    _, old, _ = project(value)
    value["history"]["cold_expiry"]["observed_at_ms"] = 1120000
    _, new, first = project(value, old, now=1121000)
    assert first == 1000000
    assert new["s6:s3"]["cold_expiry"]["first_seen_ms"] == 1121000
    assert new["s6:s3"]["worker"]["first_seen_ms"] == 1000000


@pytest.mark.parametrize("observation", [999999, 1000000, 1121001, -1, "private"])
def test_pure_old_or_invalid_current_snapshot_cannot_clear_newer_fault(observation):
    _, old, _ = project(bad())
    _, new, first = project(
        {"status": "CURRENT"}, old, now=1121000, observed=observation
    )
    assert first == 1000000
    assert new["s6:s3"]["cold_expiry"]["status"] == "RECOVERY_UNPROVEN"


def test_pure_repeated_failed_snapshot_does_not_manufacture_new_attempt_or_recovery():
    _, old, _ = project(bad(committed=999000))
    _, _, first = project(bad(committed=999000), old, now=1121000, observed=1000000)
    assert first == 1000000


def test_pure_missing_observation_cannot_clear_known_fault_even_if_current():
    _, old, _ = project(bad())
    finding, new, first = maintenance_health_projection(
        {"s6:s3": {"status": "CURRENT"}}, old, 1121000, None
    )
    assert first == 1000000 and finding is not None
    assert new["s6:s3"]["cold_expiry"]["status"] == "RECOVERY_UNPROVEN"


@pytest.mark.parametrize("current", [{"status": "CURRENT"}, 0, 2])
def test_pure_explicit_legacy_recovery_remains_compatible(current):
    _, old, _ = project({"status": "UNAVAILABLE", "failure_type": "QueryCanceled"})
    finding, new, first = project(current, old, now=1121000)
    assert (finding, new, first) == (None, {}, None)


def test_pure_absent_consumer_is_not_proof_that_worker_recovered():
    _, old, _ = project(bad())
    finding, new, first = maintenance_health_projection({}, old, 1121000, 1121000)
    assert first == 1000000 and finding is not None
    assert new["s6:s3"]["cold_expiry"]["status"] == "RECOVERY_UNPROVEN"


def test_pure_unknown_component_is_visible_without_raw_exception_text():
    value = bad("other")
    value["component_failures"]["other"] = "private exception SQL"
    value["failure_type"] = "private exception SQL"
    finding, state, _ = project(value)
    assert list(state["s6:s3"]) == ["unknown"]
    assert "private" not in json.dumps(finding)


def write_cycle(store, key, version, now, snapshot):
    return store.change(
        key,
        expected_version=version,
        request_key=f"cycle-{version}",
        reason="QA",
        payload={
            "cycle_id": key.key,
            "started_at_ms": now,
            "finished_at_ms": now,
            "status": "CYCLE_COMPLETE",
            "phases": {"signal_maintenance": snapshot},
        },
    )


def test_health_persists_lane_identity_across_probe_restarts(database):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope),
        namespace="trading-pipeline-cycle-v1",
        key="lane-rotation",
    )
    for version, delta, failed_consumer in (
        (0, 0, "s6:s3"),
        (1, 60000, "s8:s3"),
        (2, 121000, "s6:tv_bridge"),
    ):
        now[0] = 1000000 + delta
        snapshot = {
            name: bad() if name == failed_consumer else {"status": "CURRENT"}
            for name in ("s6:s3", "s8:s3", "s6:tv_bridge")
        }
        write_cycle(store, key, version, now[0], snapshot)
        health = TradingHealth(
            database, account_id="test-account", clock_ms=lambda: now[0]
        )
        assert CODE not in health()
        report = json.loads(store.read(health.key).payload_json)
        assert report["first_seen_ms"][CODE] == now[0]
        assert report["findings"][CODE]["consumers"] == [failed_consumer]
    now[0] += 121000
    assert CODE in health()  # The same unresolved lane still escalates normally.


def test_health_commit_progress_resets_continuity_without_muting_current_failure(
    database,
):
    now = [1000000]
    health = TradingHealth(database, account_id="test-account", clock_ms=lambda: now[0])
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope),
        namespace="trading-pipeline-cycle-v1",
        key="lane-progress",
    )
    write_cycle(store, key, 0, now[0], {"s6:s3": bad(committed=999000)})
    assert CODE not in health()
    now[0] += 121000
    assert CODE in health()
    write_cycle(store, key, 1, now[0], {"s6:s3": bad(committed=now[0] - 1000)})
    assert CODE not in health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["first_seen_ms"][CODE] == now[0]
    assert CODE in report["findings"]  # Not muted; the new failure remains visible.
    assert (
        report["maintenance_lanes"]["s6:s3"]["cold_expiry"]["last_committed_at_ms"]
        == now[0] - 1000
    )


def delayed_latest_connection(database, clock, read_finished):
    """Advance a deterministic clock only while the real latest-cycle read runs."""

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            if isinstance(sql, str) and "ORDER BY (payload->>'started_at_ms')" in sql:
                clock[0] = read_finished[0]
            return self.conn.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def connect():
        with database() as conn:
            yield Connection(conn)

    return connect


def test_health_slow_read_uses_post_read_clock_only_for_maintenance(
    database, monkeypatch
):
    import services.v2_trading_health as module

    now, completed_read = [1000000], [1006000]
    health = TradingHealth(
        delayed_latest_connection(database, now, completed_read),
        account_id="test-account",
        clock_ms=lambda: now[0],
    )
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="slow-read"
    )
    original = module.signal_lag_state
    lag_times = []

    def lag_projection(
        findings, active, clear_since, expected_waits, observed, settings
    ):
        lag_times.append(observed)
        return original(
            findings, active, clear_since, expected_waits, observed, settings
        )

    monkeypatch.setattr(module, "signal_lag_state", lag_projection)
    # This completion is after probe entry, but before its DB read completes.
    write_cycle(store, key, 0, 1005000, {"s6:s3": bad(committed=1004000)})
    assert CODE not in health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["observed_at_ms"] == 1000000
    assert report["maintenance_observed_at_ms"] == 1006000
    lane = report["maintenance_lanes"]["s6:s3"]["cold_expiry"]
    assert lane["first_seen_ms"] == 1006000
    assert lane["last_observed_at_ms"] == 1005000
    assert lane["last_committed_at_ms"] == 1004000
    assert lag_times == [1000000]

    # Crossing the maintenance threshold during the read must not use the old
    # entry clock (119.5s) to postpone a mature 120s failure.
    now[0], completed_read[0] = 1125500, 1126000
    assert CODE in health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["observed_at_ms"] == 1125500
    assert report["maintenance_observed_at_ms"] == 1126000
    assert report["first_seen_ms"][CODE] == 1006000
    assert lag_times[-1] == 1125500

    # A genuinely newer CURRENT cycle arriving during a later slow read can
    # prove recovery; it must no longer be mistaken for a future timestamp.
    now[0], completed_read[0] = 1127000, 1133000
    write_cycle(store, key, 1, 1132000, {"s6:s3": {"status": "CURRENT"}})
    assert CODE not in health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["maintenance_lanes"] == {}
    assert report["observed_at_ms"] == 1127000
    assert report["maintenance_observed_at_ms"] == 1133000
    assert lag_times[-1] == 1127000


@pytest.mark.parametrize("future", ["cycle", "commit"])
def test_health_post_read_clock_still_rejects_truly_future_evidence(database, future):
    now, completed_read = [1000000], [1000000]
    health = TradingHealth(
        delayed_latest_connection(database, now, completed_read),
        account_id="test-account",
        clock_ms=lambda: now[0],
    )
    store = BusinessState(database)
    key = StateKey(
        **asdict(health.scope),
        namespace="trading-pipeline-cycle-v1",
        key="future-evidence",
    )
    write_cycle(store, key, 0, now[0], {"s6:s3": bad(committed=999000)})
    assert CODE not in health()
    now[0], completed_read[0] = 1120000, 1126000
    snapshot = {"status": "CURRENT"} if future == "cycle" else bad(committed=1126001)
    write_cycle(
        store, key, 1, 1126001 if future == "cycle" else 1125000, {"s6:s3": snapshot}
    )
    assert CODE in health()
    report = json.loads(store.read(health.key).payload_json)
    assert report["maintenance_observed_at_ms"] == 1126000
    assert report["first_seen_ms"][CODE] == 1000000
    assert (
        report["maintenance_lanes"]["s6:s3"]["cold_expiry"]["last_committed_at_ms"]
        == 999000
    )
