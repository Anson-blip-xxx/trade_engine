"""The active projection preserves task authority across churn and concurrency.

All writes use the guarded, temporary PostgreSQL fixture. The historical-load
case explains the scheduler's actual claim, rather than a second SQL model.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal, strategy_worker
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture


def insert_task(conn, consumer, signal_id, *, completed=False):
    conn.execute(
        """INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at)
        VALUES (%s,%s,CASE WHEN %s THEN clock_timestamp() ELSE NULL END)""",
        (consumer, signal_id, completed),
    )


def ready(conn):
    return set(
        conn.execute(
            "SELECT consumer,signal_id::text FROM v2_strategy_ready"
        ).fetchall()
    )


def assert_projection(conn):
    assert ready(conn) == set(
        conn.execute(
            """SELECT consumer,signal_id::text FROM v2_strategy_tasks
            WHERE completed_at IS NULL"""
        ).fetchall()
    )


def disable_projection(conn):
    conn.execute("UPDATE v2_strategy_ready_control SET enabled=false")


def ignored_worker(connect, *, producer="s6"):
    return strategy_worker(
        connect,
        producer=producer,
        decide=lambda *_: StrategyDecision("IGNORED", "QA_READY_QUEUE"),
    )[0]


@contextmanager
def bounded_connection(database):
    with database() as conn:
        conn.execute("SET LOCAL statement_timeout='2s'")
        yield conn


def recording_database(database, claims):
    @contextmanager
    def connect():
        with database() as conn:

            class RecordingConnection:
                def execute(self, query, *args, **kwargs):
                    if isinstance(query, str) and "attempts=attempts+1" in query:
                        claims.append((query, args[0]))
                    return conn.execute(query, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield RecordingConnection()

    return connect


def test_empty_schema_has_validated_projection_and_always_enabled_mirror(database):
    with database() as conn:
        assert conn.execute(
            """SELECT singleton,enabled,validated_at IS NOT NULL
            FROM v2_strategy_ready_control"""
        ).fetchall() == [(True, True, True)]
        mirrors = conn.execute(
            """SELECT t.tgenabled FROM pg_trigger t
            WHERE t.tgrelid='v2_strategy_tasks'::regclass AND NOT t.tgisinternal
            AND pg_get_functiondef(t.tgfoid) LIKE '%%v2_strategy_ready%%'"""
        ).fetchall()
        assert mirrors and all(row == ("A",) for row in mirrors)
        assert_projection(conn)


def test_membership_tracks_insert_completion_reopen_and_delete(database):
    pending = scheduler_signal(database, "ready-lifecycle")
    terminal = scheduler_signal(database, "ready-already-terminal")
    with database() as conn:
        insert_task(conn, "qa-consumer", pending)
        insert_task(conn, "qa-consumer", terminal, completed=True)
        assert ready(conn) == {("qa-consumer", pending)}
        conn.execute(
            """UPDATE v2_strategy_tasks SET completed_at=clock_timestamp()
            WHERE signal_id=%s""",
            (pending,),
        )
        assert ready(conn) == set()
        conn.execute(
            "UPDATE v2_strategy_tasks SET completed_at=NULL WHERE signal_id=%s",
            (terminal,),
        )
        assert ready(conn) == {("qa-consumer", terminal)}
        conn.execute("DELETE FROM v2_strategy_tasks WHERE signal_id=%s", (terminal,))
        assert_projection(conn)
        assert ready(conn) == set()


def test_lease_and_retry_updates_do_not_rewrite_existing_ready_tuple(database):
    signal_id = scheduler_signal(database, "ready-no-churn")
    with database() as conn:
        insert_task(conn, "qa-consumer", signal_id)
    with database() as conn:
        before = conn.execute(
            "SELECT xmin::text,ctid::text FROM v2_strategy_ready"
        ).fetchone()
        conn.execute(
            """UPDATE v2_strategy_tasks SET attempts=attempts+1,
            next_attempt_at=clock_timestamp()+interval '1 second',
            completed_at=completed_at WHERE signal_id=%s""",
            (signal_id,),
        )
    with database() as conn:
        assert (
            conn.execute(
                "SELECT xmin::text,ctid::text FROM v2_strategy_ready"
            ).fetchone()
            == before
        )
        assert_projection(conn)


@pytest.mark.parametrize("operation", ["insert", "complete", "reopen", "delete"])
def test_task_transaction_rollback_also_rolls_back_projection(database, operation):
    signal_id = scheduler_signal(database, "ready-rollback-" + operation)
    if operation != "insert":
        with database() as conn:
            insert_task(conn, "qa-consumer", signal_id, completed=operation == "reopen")
    with database() as conn:
        before = ready(conn)

    class AbortAttempt(Exception):
        pass

    with pytest.raises(AbortAttempt), database() as conn:
        if operation == "insert":
            insert_task(conn, "qa-consumer", signal_id)
        elif operation == "complete":
            conn.execute("UPDATE v2_strategy_tasks SET completed_at=clock_timestamp()")
        elif operation == "reopen":
            conn.execute("UPDATE v2_strategy_tasks SET completed_at=NULL")
        else:
            conn.execute("DELETE FROM v2_strategy_tasks")
        assert_projection(conn)
        raise AbortAttempt
    with database() as conn:
        assert ready(conn) == before
        assert_projection(conn)


def test_membership_is_isolated_by_consumer_for_same_signal(database):
    signal_id = scheduler_signal(database, "ready-shared-signal")
    with database() as conn:
        for consumer in ("qa-first", "qa-second"):
            insert_task(conn, consumer, signal_id)
        conn.execute(
            """UPDATE v2_strategy_tasks SET completed_at=clock_timestamp()
            WHERE consumer='qa-first'"""
        )
        assert ready(conn) == {("qa-second", signal_id)}
        conn.execute("DELETE FROM v2_strategy_tasks WHERE consumer='qa-first'")
        assert_projection(conn)


def test_completed_replay_keeps_history_without_reentering_ready(database):
    signal_id = scheduler_signal(database, "ready-terminal-replay")
    calls = []
    worker = ignored_worker(database)
    scheduler = StrategyScheduler(
        worker, context_provider=lambda snapshot: calls.append(snapshot) or {}
    )
    assert scheduler.run_once() == {signal_id: "IGNORED"}
    assert scheduler.run_once() == {}
    with database() as conn:
        conn.execute(
            """INSERT INTO v2_strategy_tasks(consumer,signal_id)
            VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (worker.scope.consumer, signal_id),
        )
        assert ready(conn) == set()
        assert conn.execute(
            "SELECT attempts,completed_at IS NOT NULL FROM v2_strategy_tasks"
        ).fetchone() == (1, True)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            1,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("corruption", ["missing", "extra", "equal-count-swap"])
def test_enable_requires_exact_membership_not_only_matching_counts(
    database, corruption
):
    pending = scheduler_signal(database, "ready-gate-pending")
    terminal = scheduler_signal(database, "ready-gate-terminal")
    with database() as conn:
        insert_task(conn, "qa-consumer", pending)
        insert_task(conn, "qa-consumer", terminal, completed=True)
        disable_projection(conn)
        if corruption in {"missing", "equal-count-swap"}:
            conn.execute("DELETE FROM v2_strategy_ready WHERE signal_id=%s", (pending,))
        if corruption in {"extra", "equal-count-swap"}:
            conn.execute(
                "INSERT INTO v2_strategy_ready(consumer,signal_id) VALUES (%s,%s)",
                ("qa-consumer", terminal),
            )
    with pytest.raises(psycopg.Error), database() as conn:
        conn.execute("SELECT v2_strategy_ready_enable()")
    with database() as conn:
        assert conn.execute(
            "SELECT enabled FROM v2_strategy_ready_control"
        ).fetchone() == (False,)


@pytest.mark.parametrize("mode", ["DISABLE", "ENABLE"])
def test_enable_rejects_disabled_or_replica_bypassable_mirror(database, mode):
    with database() as conn:
        mirrors = conn.execute(
            """SELECT t.tgname FROM pg_trigger t
            WHERE t.tgrelid='v2_strategy_tasks'::regclass AND NOT t.tgisinternal
            AND pg_get_functiondef(t.tgfoid) LIKE '%%v2_strategy_ready%%'"""
        ).fetchall()
        assert mirrors
        disable_projection(conn)
        for (trigger_name,) in mirrors:
            conn.execute(
                sql.SQL("ALTER TABLE v2_strategy_tasks {} TRIGGER {}").format(
                    sql.SQL(mode), sql.Identifier(trigger_name)
                )
            )
    with pytest.raises(psycopg.Error), database() as conn:
        conn.execute("SELECT v2_strategy_ready_enable()")


@pytest.mark.parametrize("shape", ["insert-only", "conditional", "update-of"])
def test_enable_and_runtime_gate_reject_same_name_mirror_with_incomplete_coverage(
    database, shape
):
    from v2_core.ready_queue import queue_mode

    events = {
        "insert-only": "INSERT",
        "conditional": "INSERT OR UPDATE OR DELETE",
        "update-of": "INSERT OR UPDATE OF completed_at OR DELETE",
    }[shape]
    condition = "WHEN (true)" if shape == "conditional" else ""
    with database() as conn:
        disable_projection(conn)
        conn.execute("DROP TRIGGER v2_strategy_ready_sync ON v2_strategy_tasks")
        conn.execute(
            sql.SQL(
                """CREATE TRIGGER v2_strategy_ready_sync AFTER {}
                ON v2_strategy_tasks FOR EACH ROW {}
                EXECUTE FUNCTION v2_strategy_ready_sync_row()"""
            ).format(sql.SQL(events), sql.SQL(condition))
        )
        conn.execute(
            "ALTER TABLE v2_strategy_tasks ENABLE ALWAYS TRIGGER v2_strategy_ready_sync"
        )
    with pytest.raises(psycopg.Error), database() as conn:
        conn.execute("SELECT v2_strategy_ready_enable()")
    # A stale activation marker cannot override invalid trigger coverage.
    with database() as conn:
        conn.execute(
            """UPDATE v2_strategy_ready_control
            SET enabled=true,validated_at=clock_timestamp()"""
        )
    assert queue_mode(database)[0] == "CANONICAL"


def test_bounded_backfill_repairs_missing_rows_then_enables(database):
    pending = {scheduler_signal(database, f"ready-backfill-{i}") for i in range(3)}
    terminal = scheduler_signal(database, "ready-backfill-complete")
    with database() as conn:
        for signal_id in pending:
            insert_task(conn, "qa-consumer", signal_id)
        insert_task(conn, "qa-consumer", terminal, completed=True)
        disable_projection(conn)
        conn.execute("DELETE FROM v2_strategy_ready")
    for expected in range(1, 4):
        with database() as conn:
            conn.execute("SELECT v2_strategy_ready_backfill(1)")
            assert len(ready(conn)) == expected
            assert conn.execute(
                "SELECT enabled FROM v2_strategy_ready_control"
            ).fetchone() == (False,)
    with database() as conn:
        conn.execute("SELECT v2_strategy_ready_backfill(1)")
        assert_projection(conn)
        assert len(ready(conn)) == 3
        conn.execute("SELECT v2_strategy_ready_enable()")
    with database() as conn:
        assert conn.execute(
            "SELECT enabled,validated_at IS NOT NULL FROM v2_strategy_ready_control"
        ).fetchone() == (True, True)


def test_repeated_migration_invalidates_old_validation_until_missing_tasks_are_backfilled(
    database,
):
    from v2_core.ready_queue import queue_mode

    existing = scheduler_signal(database, "ready-repeat-existing")
    missing = scheduler_signal(database, "ready-repeat-missing")
    terminal = scheduler_signal(database, "ready-repeat-terminal")
    task_snapshot = """SELECT t.*,t.xmin::text,t.ctid::text
        FROM v2_strategy_tasks t ORDER BY consumer,signal_id"""
    with database() as conn:
        insert_task(conn, "qa-consumer", existing)
        insert_task(conn, "qa-consumer", terminal, completed=True)
        assert conn.execute(
            "SELECT enabled,validated_at IS NOT NULL FROM v2_strategy_ready_control"
        ).fetchone() == (True, True)
        conn.execute(
            "ALTER TABLE v2_strategy_tasks DISABLE TRIGGER v2_strategy_ready_sync"
        )
        insert_task(conn, "qa-consumer", missing)
        canonical_before = conn.execute(task_snapshot).fetchall()
        assert ready(conn) == {("qa-consumer", existing)}

    migration = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20261110_strategy_ready_queue.sql"
    ).read_text()
    with database() as conn:
        conn.execute("SET LOCAL lock_timeout='500ms'")
        conn.execute("SET LOCAL statement_timeout='3s'")
        conn.execute(migration)
    with database() as conn:
        assert conn.execute(
            "SELECT enabled,validated_at FROM v2_strategy_ready_control"
        ).fetchone() == (False, None)
        assert conn.execute(
            """SELECT tgenabled FROM pg_trigger
            WHERE tgrelid='v2_strategy_tasks'::regclass
            AND tgname='v2_strategy_ready_sync'"""
        ).fetchone() == ("A",)
        assert conn.execute(task_snapshot).fetchall() == canonical_before
        assert ready(conn) == {("qa-consumer", existing)}
    assert queue_mode(database)[0] == "CANONICAL"
    with pytest.raises(psycopg.Error), database() as conn:
        conn.execute("SELECT v2_strategy_ready_enable()")

    with database() as conn:
        assert conn.execute("SELECT v2_strategy_ready_backfill(1)").fetchone() == (1,)
        assert conn.execute(task_snapshot).fetchall() == canonical_before
        assert_projection(conn)
        assert ready(conn) == {
            ("qa-consumer", existing),
            ("qa-consumer", missing),
        }
        conn.execute("SELECT v2_strategy_ready_enable()")
    assert queue_mode(database)[0] == "READY"


@pytest.mark.parametrize("complete_while_locked", [False, True])
def test_backfill_skips_locked_task_and_revisits_without_resurrecting_completion(
    database, complete_while_locked
):
    signal_id = scheduler_signal(database, "ready-backfill-locked")
    with database() as conn:
        insert_task(conn, "qa-consumer", signal_id)
        disable_projection(conn)
        conn.execute("DELETE FROM v2_strategy_ready")

    def backfill():
        with bounded_connection(database) as conn:
            conn.execute("SELECT v2_strategy_ready_backfill(1)")
            return ready(conn)

    with database() as lock:
        lock.execute("SELECT signal_id FROM v2_strategy_tasks FOR UPDATE")
        if complete_while_locked:
            lock.execute("UPDATE v2_strategy_tasks SET completed_at=clock_timestamp()")
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(backfill).result(timeout=5) == set()
    assert backfill() == (
        set() if complete_while_locked else {("qa-consumer", signal_id)}
    )
    with database() as conn:
        assert_projection(conn)
        conn.execute("SELECT v2_strategy_ready_enable()")


def test_claim_skips_locked_authoritative_task_and_claims_peer(database):
    first = scheduler_signal(database, "ready-lock-first", expires=9)
    second = scheduler_signal(database, "ready-lock-second", expires=10)
    worker = ignored_worker(lambda: bounded_connection(database))
    with database() as conn:
        for signal_id in (first, second):
            insert_task(conn, worker.scope.consumer, signal_id)

    def consume_one():
        return StrategyScheduler(worker, context_provider=lambda _: {}).run_once(1)

    with database() as lock:
        lock.execute(
            "SELECT signal_id FROM v2_strategy_tasks WHERE signal_id=%s FOR UPDATE",
            (first,),
        )
        with ThreadPoolExecutor(1) as pool:
            assert pool.submit(consume_one).result(timeout=5) == {second: "IGNORED"}
    assert consume_one() == {first: "IGNORED"}
    with database() as conn:
        assert ready(conn) == set()
        assert_projection(conn)


def test_concurrent_schedulers_share_task_leases_and_complete_each_ready_once(database):
    signal_ids = {scheduler_signal(database, f"ready-concurrent-{i}") for i in range(8)}
    context_calls = []

    def consume(_):
        return StrategyScheduler(
            ignored_worker(database),
            context_provider=lambda snapshot: context_calls.append(snapshot) or {},
        ).run_once()

    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(consume, range(4)))
    assert sum(map(len, outcomes)) == len(signal_ids)
    assert set().union(*(set(outcome) for outcome in outcomes)) == signal_ids
    assert len(context_calls) == len(signal_ids)
    with database() as conn:
        assert ready(conn) == set()
        assert conn.execute(
            "SELECT count(*),sum(attempts) FROM v2_strategy_tasks"
        ).fetchone() == (8, 8)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            8,
        )


def test_disabled_projection_falls_back_to_canonical_without_hiding_missing_ready(
    database,
):
    signal_id = scheduler_signal(database, "ready-disabled-canonical")
    worker = ignored_worker(database)
    with database() as conn:
        insert_task(conn, worker.scope.consumer, signal_id)
        disable_projection(conn)
        conn.execute("DELETE FROM v2_strategy_ready")
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once(1) == {signal_id: "IGNORED"}
    assert scheduler.last_run_diagnostics["queue_source"]["mode"] == "CANONICAL"
    with database() as conn:
        assert_projection(conn)
        conn.execute("SELECT v2_strategy_ready_enable()")


def test_ready_worker_respects_lease_held_by_canonical_worker(database, monkeypatch):
    from v2_core import scheduling

    signal_id = scheduler_signal(database, "ready-old-new-workers")
    legacy_connect = lambda: bounded_connection(database)
    original_mode = scheduling.queue_mode
    monkeypatch.setattr(
        scheduling,
        "queue_mode",
        lambda connect: (
            ("CANONICAL", "QA_LEGACY_WORKER")
            if connect is legacy_connect
            else original_mode(connect)
        ),
    )
    current = StrategyScheduler(ignored_worker(database), context_provider=lambda _: {})
    competing_results = []

    def while_legacy_lease_is_active(_):
        competing_results.append(current.run_once(1))
        return {}

    legacy = StrategyScheduler(
        ignored_worker(legacy_connect), context_provider=while_legacy_lease_is_active
    )
    assert legacy.run_once(1) == {signal_id: "IGNORED"}
    assert competing_results == [{}]
    assert current.last_run_diagnostics["queue_source"]["mode"] == "READY"
    with database() as conn:
        assert conn.execute("SELECT attempts FROM v2_strategy_tasks").fetchone() == (1,)
        assert_projection(conn)
        assert not ready(conn)


def test_committed_abandoned_lease_stays_ready_and_is_reclaimed_only_after_expiry(
    database,
):
    signal_id = scheduler_signal(database, "ready-abandoned-lease")
    worker = ignored_worker(database)
    abandoned_token = str(uuid4())
    with database() as conn:
        insert_task(conn, worker.scope.consumer, signal_id)
        conn.execute(
            """UPDATE v2_strategy_tasks SET attempts=1,lease_token=%s,
            lease_until=clock_timestamp()+interval '1 hour' WHERE signal_id=%s""",
            (abandoned_token, signal_id),
        )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert scheduler.run_once(1) == {}
    with database() as conn:
        assert ready(conn) == {(worker.scope.consumer, signal_id)}
        assert conn.execute(
            "SELECT attempts,lease_token::text FROM v2_strategy_tasks"
        ).fetchone() == (1, abandoned_token)
        conn.execute(
            "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second'"
        )
    assert scheduler.run_once(1) == {signal_id: "IGNORED"}
    with database() as conn:
        assert conn.execute(
            "SELECT attempts,lease_token,completed_at IS NOT NULL FROM v2_strategy_tasks"
        ).fetchone() == (2, None, True)
        assert not ready(conn)


def test_claim_locks_only_the_selected_task_not_every_ready_candidate(database):
    signal_ids = {scheduler_signal(database, f"ready-lock-scope-{i}") for i in range(4)}
    claims = []
    worker = ignored_worker(recording_database(database, claims))
    with database() as conn:
        for signal_id in signal_ids:
            insert_task(conn, worker.scope.consumer, signal_id)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert len(scheduler.run_once(1)) == 1
    assert len(claims) == 1
    with database() as conn:
        conn.execute("UPDATE v2_strategy_tasks SET completed_at=NULL")
        assert len(ready(conn)) == 4
    query, params = claims[0]
    with database() as holding_claim:
        selected = holding_claim.execute(query, params).fetchone()[0]
        with bounded_connection(database) as other:
            unlocked = other.execute(
                """SELECT signal_id::text FROM v2_strategy_tasks
                WHERE consumer=%s AND signal_id<>%s FOR UPDATE NOWAIT""",
                (worker.scope.consumer, selected),
            ).fetchall()
            assert {row[0] for row in unlocked} == signal_ids - {selected}
            assert (
                other.execute(
                    """SELECT signal_id FROM v2_strategy_tasks
                WHERE consumer=%s AND signal_id=%s FOR UPDATE SKIP LOCKED""",
                    (worker.scope.consumer, selected),
                ).fetchall()
                == []
            )
        # A failed process before committing a claim leaves no new lease.
        holding_claim.rollback()
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE lease_token IS NOT NULL"
        ).fetchone() == (0,)
        assert_projection(conn)


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def test_claim_plan_stays_on_ready_and_primary_key_with_large_completed_history(
    database,
):
    claims = []
    worker = ignored_worker(recording_database(database, claims))
    with database() as conn:
        conn.execute(
            """WITH signals AS (
              INSERT INTO v2_inbound_signals
                (signal_id,source,environment,request_key,snapshot)
              SELECT md5('ready-history-'||n::text)::uuid,'tv_bridge','SANDBOX',
                'ready-history-'||n::text,
                jsonb_build_object('observed_at',1,'expires_at_ms',2,
                  'symbol','ETHUSDT','signal','TREND_UP','features','{}'::jsonb)
              FROM generate_series(1,20000) n RETURNING signal_id
            ) INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at)
              SELECT %s,signal_id,clock_timestamp() FROM signals""",
            (worker.scope.consumer,),
        )
    live = {scheduler_signal(database, f"ready-live-{i}") for i in range(4)}
    with database() as conn:
        for signal_id in live:
            insert_task(conn, worker.scope.consumer, signal_id)
        for table in ("v2_strategy_tasks", "v2_strategy_ready", "v2_inbound_signals"):
            conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
        assert len(ready(conn)) == 4
    result = StrategyScheduler(worker, context_provider=lambda _: {}).run_once(
        1, hot_only=True
    )
    assert len(result) == 1 and set(result) <= live
    assert len(claims) == 1
    with database() as conn:
        query, params = claims[0]
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()[0][0]
        nodes = list(plan_nodes(plan["Plan"]))
        assert any(node.get("Relation Name") == "v2_strategy_ready" for node in nodes)
        assert not any(
            node.get("Index Name") == "v2_strategy_tasks_due" for node in nodes
        )
        task_scans = [
            node
            for node in nodes
            if node.get("Relation Name") == "v2_strategy_tasks"
            and node["Node Type"] != "ModifyTable"
        ]
        assert task_scans
        assert all(
            node["Node Type"] in {"Index Scan", "Index Only Scan"}
            for node in task_scans
        )
        assert all(
            node.get("Index Name") == "v2_strategy_tasks_pkey" for node in task_scans
        )
        assert all("signal_id" in node.get("Index Cond", "") for node in task_scans)
        assert len(ready(conn)) == 3
        assert_projection(conn)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE completed_at IS NOT NULL"
        ).fetchone() == (20001,)
