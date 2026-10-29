"""Pending-only lookups retain receipt authority, locking and health semantics."""

import json
import time
from contextlib import contextmanager

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal, strategy_worker
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_trading_health import TradingHealth
from v2_core.runtime_policy import resolve
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture
case = pipeline_case


def test_health_distinguishes_expired_live_lease_and_backoff_without_consuming(
    database,
):
    now = time.time_ns() // 1000000 + 31000
    ids = [
        scheduler_signal(database, name, expires=expiry)
        for name, expiry in (
            ("expired", 2),
            ("leased", now + 60000),
            ("backoff", now + 60000),
        )
    ]
    worker, _ = strategy_worker(database)
    with database() as c:
        c.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) "
            "VALUES (%s,%s,'00000000-0000-0000-0000-000000000001',clock_timestamp()+interval '2 minutes')",
            (worker.scope.consumer, ids[1]),
        )
        c.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,next_attempt_at,error_code) "
            "VALUES (%s,%s,clock_timestamp()+interval '2 minutes','EXISTING_SYMBOL_POSITION')",
            (worker.scope.consumer, ids[2]),
        )
    health = TradingHealth(
        database, account_id="test-account", tv_enabled=True, clock_ms=lambda: now
    )
    assert "SIGNAL_CONSUMPTION_LAG" in health()
    report = json.loads(health.store.read(health.key).payload_json)
    diag = report["queue_diagnostics"]["s6:tv_bridge"]
    assert diag == {
        "query_ms": diag["query_ms"],
        "scope": "OVER_LAG_THRESHOLD_UNRECEIPTED",
        "valid_actionable": 2,
        "expired_actionable": 1,
        "leased_actionable": 1,
        "backoff_actionable": 1,
        "valid_lag": 1,
        "expired_bookkeeping_lag": 1,
        "admission_read_quota_wait": 0,
        "account_capacity_wait": 1,
        "candidate_dependency_wait": 0,
        "reason_codes": {"EXISTING_SYMBOL_POSITION": 1},
    }
    assert type(diag["query_ms"]) is int and diag["query_ms"] >= 0
    assert report["findings"]["SIGNAL_CONSUMPTION_LAG"]["s6:tv_bridge"]["count"] == 2
    assert report["expected_waits"]["s6:tv_bridge"]["count"] == 1
    with database() as c:
        assert c.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_receipts_not_task_completion_are_pending_authority(database):
    sid = scheduler_signal(database, "unfinished-with-stale-completion", expires=2)
    worker, _ = strategy_worker(database)
    with database() as c:
        c.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at) VALUES (%s,%s,clock_timestamp())",
            (worker.scope.consumer, sid),
        )
    health = TradingHealth(
        database,
        account_id="test-account",
        tv_enabled=True,
        clock_ms=lambda: time.time_ns() // 1000000 + 31000,
    )
    assert "SIGNAL_CONSUMPTION_LAG" in health()
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: pytest.fail("no network")
    )
    assert scheduler.expire_pending() == 1
    assert "SIGNAL_CONSUMPTION_LAG" not in health()


def test_expiry_lateral_lookup_keeps_skip_locked_and_batch_bounds(database):
    ids = [scheduler_signal(database, f"locked-{i}", expires=2) for i in range(3)]
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    with database() as other:
        other.execute(
            "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE", (ids[0],)
        )
        assert scheduler.expire_pending(limit=1) == 1
        assert scheduler.expire_pending(limit=1) == 1
        assert scheduler.expire_pending(limit=1) == 0
        assert other.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s", (ids[0],)
        ).fetchone() == (0,)
    assert scheduler.expire_pending(limit=1) == 1


def test_cold_discovery_uses_narrow_ids_before_snapshot_and_preserves_priority(
    database,
):
    old = scheduler_signal(database, "cold-old", expires=2)
    fresh = scheduler_signal(database, "cold-new", expires=100)
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    captured = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, query, params=None):
            if "WITH undiscovered AS MATERIALIZED" in query:
                captured.append(query)
            return self.conn.execute(query, params)

    @contextmanager
    def connect():
        with database() as c:
            yield Connection(c)

    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    scheduler._connect = connect
    assert scheduler.run_once(limit=1) == {fresh: "IGNORED"}
    scheduler._last_reconcile_ms = None
    assert scheduler.run_once(limit=1) == {old: "EXPIRED"}
    assert len(captured) == 2
    assert all("CROSS JOIN LATERAL" in sql and "OFFSET 0) s" in sql for sql in captured)


def test_every_pipeline_action_has_integer_timing_even_when_blocked(case):
    pipeline, *_ = case
    pipeline.exits.status = "BLOCKED"
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert set(result["phase_elapsed_ms"]) == set(result["phases"])
    assert all(type(v) is int and v >= 0 for v in result["phase_elapsed_ms"].values())
    assert result["entries"] == {}


@pytest.mark.parametrize("raises", [False, True])
def test_pipeline_owner_releases_before_connection_cleanup(case, database, raises):
    pipeline, *_ = case
    with database() as retained:

        @contextmanager
        def delayed_close():
            yield retained

        pipeline.connect = delayed_close
        if raises:
            with (
                pytest.raises(ValueError, match="QA_INTERRUPTED"),
                pipeline._ownership() as acquired,
            ):
                assert acquired
                raise ValueError("QA_INTERRUPTED")
        else:
            with pipeline._ownership() as acquired:
                assert acquired
        # Retain the old backend deliberately. A second owner must not depend
        # on connection teardown completing before it can protect the account.
        with database() as next_owner:
            assert next_owner.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                ("v2-pipeline:" + pipeline.scope.key,),
            ).fetchone() == (True,)
            # A contending pipeline cannot release another owner's lock.
            with pipeline._ownership() as acquired:
                assert not acquired
            assert next_owner.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                ("v2-pipeline:" + pipeline.scope.key,),
            ).fetchone() == (True,)


def test_hot_expiry_and_periodic_full_sweep_keep_late_commits(database):
    now = [1000000]
    worker, _ = strategy_worker(database, now=lambda: now[0])
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: pytest.fail("no I/O")
    )
    assert scheduler.expire_pending(reconcile_seconds=60) == 0
    assert scheduler.last_expiry_metrics["full_reconciliation"]
    old = scheduler_signal(database, "late-old-expired", expires=2)
    recent = scheduler_signal(database, "late-recent-expired", expires=now[0] - 1)
    assert scheduler.expire_pending(reconcile_seconds=60) == 1
    assert not scheduler.last_expiry_metrics["full_reconciliation"]
    with database() as c:
        assert c.execute(
            "SELECT signal_id::text FROM v2_signal_receipts"
        ).fetchall() == [(recent,)]
    now[0] += 60000
    assert scheduler.expire_pending(reconcile_seconds=60) == 1
    assert scheduler.last_expiry_metrics["full_reconciliation"]
    with database() as c:
        assert c.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE signal_id=%s", (old,)
        ).fetchone() == ("EXPIRED",)


def test_full_expiry_batch_does_not_park_old_backlog(database):
    worker, _ = strategy_worker(database, now=lambda: 1000000)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    for i in range(3):
        scheduler_signal(database, f"old-backlog-{i}", expires=2)
    for _ in range(3):
        assert scheduler.expire_pending(limit=1, reconcile_seconds=3600) == 1
        assert scheduler.last_expiry_metrics["full_reconciliation"]
        assert scheduler._last_expiry_reconcile_ms is None
    assert scheduler.expire_pending(limit=1, reconcile_seconds=3600) == 0
    assert scheduler._last_expiry_reconcile_ms == 1000000


@pytest.mark.parametrize("recovery", ["restart", "clock_rollback", "shorter_policy"])
def test_expiry_reconciliation_recovers_without_cursor(database, recovery):
    now = [1000000]
    worker, _ = strategy_worker(database, now=lambda: now[0])
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    scheduler.expire_pending(reconcile_seconds=60)
    scheduler_signal(database, "late-after-full", expires=2)
    if recovery == "restart":
        scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    elif recovery == "clock_rollback":
        now[0] -= 1
    else:
        now[0] += 1000
    assert (
        scheduler.expire_pending(
            reconcile_seconds=1 if recovery == "shorter_policy" else 60
        )
        == 1
    )
    assert scheduler.last_expiry_metrics["full_reconciliation"]


@pytest.mark.parametrize("value", [0, -1, True, 3601, 1.5])
def test_expiry_reconciliation_policy_bounds(value):
    with pytest.raises((ValueError, TypeError)):
        resolve({"scheduler.discovery_reconcile_seconds": value})
