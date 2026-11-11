"""Queue maintenance changes storage policy, never pending work or leases."""

from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import LockNotAvailable

from tests.support.v2_admission import database, scheduler_signal, strategy_worker
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

__all__ = ["database"]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db/migrations/20261110_strategy_task_autovacuum.sql"
).read_text()
OPTIONS = {
    "autovacuum_vacuum_scale_factor=0.01",
    "autovacuum_vacuum_threshold=10000",
}


def options(c):
    return set(
        c.execute(
            "SELECT reloptions FROM pg_class WHERE oid='v2_strategy_tasks'::regclass"
        ).fetchone()[0]
        or ()
    )


def task_rows(c):
    return c.execute("SELECT * FROM v2_strategy_tasks ORDER BY signal_id").fetchall()


def test_new_schema_has_queue_specific_vacuum_trigger(database):
    with database() as c:
        assert options(c) == {
            "autovacuum_vacuum_scale_factor=0",
            "autovacuum_vacuum_threshold=100000",
        }


def test_migration_is_repeatable_and_preserves_all_task_states(database):
    ids = [scheduler_signal(database, f"queue-policy-{n}") for n in range(3)]
    worker, _ = strategy_worker(database)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    scheduler.run_once(discover_only=True)
    with database() as c:
        c.execute(
            "UPDATE v2_strategy_tasks SET lease_token=%s,"
            "lease_until=clock_timestamp()+interval '1 minute',attempts=1 "
            "WHERE signal_id=%s",
            (str(uuid4()), ids[1]),
        )
        c.execute(
            "UPDATE v2_strategy_tasks SET completed_at=clock_timestamp() "
            "WHERE signal_id=%s",
            (ids[2],),
        )
        c.execute(
            "ALTER TABLE v2_strategy_tasks RESET "
            "(autovacuum_vacuum_scale_factor,autovacuum_vacuum_threshold)"
        )
        before = task_rows(c)
        definition = c.execute(
            "SELECT pg_get_indexdef('v2_strategy_tasks_due'::regclass)"
        ).fetchone()
    for _ in range(2):
        with database() as c:
            c.execute("SET LOCAL lock_timeout='100ms'")
            c.execute(MIGRATION)
            assert options(c) == OPTIONS
            assert task_rows(c) == before
            assert c.execute(
                "SELECT pg_get_indexdef('v2_strategy_tasks_due'::regclass)"
            ).fetchone() == definition


def test_migration_allows_an_existing_read_transaction(database):
    with database() as reader:
        reader.execute("SELECT * FROM v2_strategy_tasks LIMIT 1")
        with database() as migration:
            migration.execute("SET LOCAL lock_timeout='100ms'")
            migration.execute(MIGRATION)
        assert options(reader) == OPTIONS


def test_competing_maintenance_aborts_migration_with_bounded_lock_wait(database):
    with database() as c:
        c.execute(
            "ALTER TABLE v2_strategy_tasks RESET "
            "(autovacuum_vacuum_scale_factor,autovacuum_vacuum_threshold)"
        )
    with database() as maintenance:
        # Storage-policy ALTER uses SHARE UPDATE EXCLUSIVE, compatible with
        # ordinary reads/writes but not another maintenance/schema operation.
        maintenance.execute("LOCK v2_strategy_tasks IN SHARE UPDATE EXCLUSIVE MODE")
        with pytest.raises(LockNotAvailable), database() as migration:
            migration.execute("SET LOCAL lock_timeout='100ms'")
            migration.execute(MIGRATION)
        assert options(maintenance) == set()
    with database() as c:
        c.execute(MIGRATION)
        assert options(c) == OPTIONS


def test_consume_and_receipt_continue_after_migration(database):
    sid = scheduler_signal(database, "queue-policy-consume")
    with database() as c:
        c.execute(MIGRATION)
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once(1)[sid] == "IGNORED"
    with database() as c:
        assert c.execute(
            "SELECT completed_at IS NOT NULL,lease_token IS NULL "
            "FROM v2_strategy_tasks WHERE signal_id=%s",
            (sid,),
        ).fetchone() == (True, True)
        assert c.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE signal_id=%s", (sid,)
        ).fetchone() == ("IGNORED",)
