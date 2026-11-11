"""Bounded cyclic bookkeeping preserves late commits and trading authority."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal, strategy_worker
from v2_core.scheduling import StrategyScheduler
from v2_core.signals import Signals
from v2_core.strategy import StrategyDecision

database = database_fixture


def scheduler(database, **kwargs):
    worker, _ = strategy_worker(database, **kwargs)
    return StrategyScheduler(
        worker, context_provider=lambda _: pytest.fail("bookkeeping cannot evaluate")
    )


def step(target, mode, *, size=2, limit=1000, budget_ms=None):
    if mode == "expiry":
        return target.expire_pending(
            limit=limit, history_page_size=size, budget_ms=budget_ms
        )
    return target.run_once(
        limit=limit, discover_only=True, history_page_size=size, budget_ms=budget_ms
    )


def metrics(target, mode):
    return target.last_expiry_metrics if mode == "expiry" else target.last_run_metrics


def cursor(target, mode):
    return getattr(
        target, f"_{'expiry' if mode == 'expiry' else 'discovery'}_history_cursor"
    )


def completed(database, target, mode):
    table = "v2_signal_receipts" if mode == "expiry" else "v2_strategy_tasks"
    with database() as conn:
        return {
            str(row[0])
            for row in conn.execute(
                f"SELECT signal_id FROM {table} WHERE consumer=%s",
                (target.worker.scope.consumer,),
            )
        }


def seed_done(database, target, identifiers):
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "SELECT %s,unnest(%s::uuid[]),'EXPIRED','QA historical receipt'",
            (target.worker.scope.consumer, identifiers),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at) "
            "SELECT %s,unnest(%s::uuid[]),clock_timestamp()",
            (target.worker.scope.consumer, identifiers),
        )


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
@pytest.mark.parametrize("size", [0, -1, True, 1.5, "100", 10001])
def test_invalid_page_size_is_rejected_before_database(mode, size):
    target = StrategyScheduler(
        SimpleNamespace(_connect=lambda: pytest.fail("must reject before DB")),
        context_provider=lambda _: {},
    )
    with pytest.raises(ValueError, match="HISTORY_PAGE_SIZE_INVALID"):
        step(target, mode, size=size)


@pytest.mark.parametrize(
    "method,options,error",
    [
        ("expire_pending", {"hot_only": True, "reconcile_seconds": 60}, "COLD_EXPIRY"),
        ("run_once", {}, "COLD_DISCOVERY"),
        ("run_once", {"hot_only": True, "discover_only": True}, "COLD_DISCOVERY"),
    ],
)
def test_history_pages_cannot_run_on_foreground_or_hot_paths(method, options, error):
    target = StrategyScheduler(
        SimpleNamespace(_connect=lambda: pytest.fail("must reject before DB")),
        context_provider=lambda _: {},
    )
    with pytest.raises(ValueError, match=error):
        getattr(target, method)(history_page_size=1, **options)


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_done_history_pages_advance_without_false_caught_up(database, mode):
    target = scheduler(database)
    identifiers = [
        scheduler_signal(database, f"history-{number:03}", expires=2)
        for number in range(7)
    ]
    seed_done(database, target, identifiers[:6])
    for _ in range(3):
        step(target, mode)
        report = metrics(target, mode)
        assert report["history_scanned"] == 2
        assert report["history_sweep_complete"] is False
        assert report["history_cursor_present"] is True
        assert report["history_sweeps"] == 0
        assert "caught_up" not in report
        assert identifiers[-1] not in completed(database, target, mode)
    step(target, mode)
    report = metrics(target, mode)
    assert completed(database, target, mode) == set(identifiers)
    assert report["history_scanned"] == 1
    assert report["history_sweep_complete"] is True
    assert report["history_cursor_present"] is False
    assert report["history_sweeps"] == 1
    assert report["history_page"] == {
        "scanned": 1,
        "page_size": 2,
        "sweep_complete": True,
        "cursor_present": False,
        "elapsed_ms": report["history_elapsed_ms"],
        "sweeps": 1,
    }
    assert "history-" not in str(report)


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_late_committed_row_behind_cursor_is_revisited_after_wrap(database, mode):
    target = scheduler(database)
    before = [scheduler_signal(database, key, expires=2) for key in ("b", "c", "d")]
    step(target, mode)
    assert cursor(target, mode) == "c"
    late = scheduler_signal(database, "a", expires=2)
    step(target, mode)
    assert metrics(target, mode)["history_sweep_complete"]
    assert late not in completed(database, target, mode)
    step(target, mode)
    assert set(before) | {late} == completed(database, target, mode)


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_output_limit_caps_page_without_skipping_eligible_rows(database, mode):
    target = scheduler(database)
    identifiers = {
        scheduler_signal(database, str(number), expires=2) for number in range(3)
    }
    for number in range(3):
        step(target, mode, size=1000, limit=1)
        assert metrics(target, mode)["history_page_size"] == 1
        assert metrics(target, mode)["history_scanned"] == 1
        assert len(completed(database, target, mode)) == number + 1
    assert completed(database, target, mode) == identifiers
    step(target, mode, size=1000, limit=1)
    assert metrics(target, mode)["history_sweep_complete"]


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
@pytest.mark.parametrize("failure", ["query", "commit", "commit_ack"])
def test_last_cursor_commit_failure_retries_same_page(database, mode, failure):
    target = scheduler(database)
    identifiers = [scheduler_signal(database, key, expires=2) for key in ("a", "b")]
    step(target, mode, size=1)
    original = cursor(target, mode)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            if failure == "query" and sql.startswith("WITH "):
                from psycopg.errors import QueryCanceled

                raise QueryCanceled("QA query budget")
            return self.conn.execute(sql, args)

    @contextmanager
    def failing():
        with database() as conn:
            yield Connection(conn)
            if failure == "commit":
                raise ConnectionError("QA commit failed")
        if failure == "commit_ack":
            raise ConnectionError("QA commit acknowledgment lost")

    target._connect = failing
    with pytest.raises(Exception, match="QA"):
        step(target, mode, size=1)
    assert cursor(target, mode) == original
    assert target._expiry_history_sweeps == target._discovery_history_sweeps == 0
    assert (identifiers[1] in completed(database, target, mode)) is (
        failure == "commit_ack"
    )
    target._connect = database
    step(target, mode, size=1)
    assert cursor(target, mode) == "b"
    assert completed(database, target, mode) == set(identifiers)


def test_expiry_revisits_released_lease_and_skip_locked_row(database):
    target = scheduler(database)
    leased = scheduler_signal(database, "a", expires=2)
    locked = scheduler_signal(database, "b", expires=2)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) "
            "VALUES(%s,%s,%s,clock_timestamp()+interval '10 minutes')",
            (target.worker.scope.consumer, leased, str(uuid4())),
        )
    with database() as owner:
        owner.execute(
            "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE", (locked,)
        )
        assert step(target, "expiry") == 0
        assert completed(database, target, "expiry") == set()
    with database() as conn:
        conn.execute(
            "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE consumer=%s AND signal_id=%s",
            (target.worker.scope.consumer, leased),
        )
    assert step(target, "expiry") == 0  # Exhaustion resets the ring.
    assert metrics(target, "expiry")["history_sweep_complete"]
    assert step(target, "expiry") == 2
    assert completed(database, target, "expiry") == {leased, locked}


def test_paginated_expiry_rechecks_lease_renewed_after_candidate_lock(database):
    target = scheduler(database)
    identifier = scheduler_signal(database, "renewed", expires=2)
    consumer = target.worker.scope.consumer
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) "
            "VALUES(%s,%s,%s,clock_timestamp()-interval '1 second')",
            (consumer, identifier, str(uuid4())),
        )
    renewals = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            result = self.conn.execute(sql, args)
            if sql.startswith("WITH pending AS MATERIALIZED"):
                assert "statement_timestamp()" in sql and "clock_timestamp()" not in sql
                with database() as other:
                    other.execute(
                        "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()+interval '10 minutes' "
                        "WHERE consumer=%s AND signal_id=%s",
                        (consumer, identifier),
                    )
                renewals.append(identifier)
            return result

    @contextmanager
    def interleaved():
        with database() as conn:
            yield Connection(conn)

    target._connect = interleaved
    assert step(target, "expiry") == 0
    assert renewals == [identifier]
    assert completed(database, target, "expiry") == set()


def test_decisions_and_same_consumer_intents_remain_protected(database):
    from tests.support.v2_admission import signal_request

    service, request, proof, own = signal_request(database)
    assert service.accept(request, proof).code == "ACCEPTED"
    target = scheduler(database, now=lambda: 100)
    assert step(target, "expiry", size=100) == 0
    assert own not in completed(database, target, "expiry")
    other = scheduler(database, producer="s8", now=lambda: 100)
    assert step(other, "expiry", size=100) == 1
    assert completed(database, other, "expiry") == {own}

    decided = scheduler_signal(database, "decided", observed=100, expires=200)
    target.worker.decide = lambda *_: StrategyDecision("IGNORED", "QA decided")
    assert target.worker.consume(decided, context={})["status"] == "IGNORED"
    target.worker.runtime._now = lambda: 300
    assert step(target, "expiry", size=100) == 0
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (target.worker.scope.consumer, decided),
        ).fetchone() == ("IGNORED",)


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_pages_remain_source_environment_and_consumer_scoped(database, mode):
    target = scheduler(database)
    own = scheduler_signal(database, "a", expires=2)
    seed_done(database, target, [own])
    for source, environment in (("s3", "SANDBOX"), ("tv_bridge", "LIVE")):
        Signals(database).admit(
            source=source,
            environment=environment,
            request_key="a",
            snapshot={
                "observed_at": 1,
                "expires_at_ms": 2,
                "symbol": "ETHUSDT",
                "signal": "TREND_UP",
                "features": {},
            },
        )
    other = scheduler(database)
    other.worker.scope = replace(other.worker.scope, account_id="other-account")
    step(other, mode, size=100)
    assert metrics(other, mode)["history_scanned"] == 1
    assert completed(database, other, mode) == {own}
    assert completed(database, target, mode) == {own}


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_raw_page_limit_precedes_bounded_id_probes(database, mode):
    target = scheduler(database)
    for key in ("a", "b", "c"):
        scheduler_signal(database, key, expires=2)
    captured = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            captured.append((sql, args))
            return self.conn.execute(sql, args)

    @contextmanager
    def observe():
        with database() as conn:
            yield Connection(conn)

    target._connect = observe
    step(target, mode)
    page = next(
        i
        for i, (sql, _) in enumerate(captured)
        if sql.startswith("SELECT signal_id,request_key")
    )
    probe = next(i for i, (sql, _) in enumerate(captured) if sql.startswith("WITH "))
    assert page < probe
    assert "ORDER BY request_key LIMIT %s" in captured[page][0]
    assert "NOT EXISTS" not in captured[page][0]
    assert captured[page][1][-1] == 2
    early = captured[probe][0].split("CROSS JOIN LATERAL", 1)[0]
    assert "unnest(%s::uuid[])" in early
    assert "v2_inbound_signals" not in early
    assert "snapshot" not in early
    assert len(captured[probe][1][0]) == 2
    assert "OFFSET 0" in early
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)


def test_hot_expiry_and_discovery_have_independent_history_cursors(database):
    target = scheduler(database)
    identifiers = [
        scheduler_signal(database, key, expires=2) for key in ("a", "b", "c")
    ]
    step(target, "discovery", size=1)
    assert cursor(target, "discovery") == "a"
    assert cursor(target, "expiry") is None
    step(target, "expiry", size=2)
    assert cursor(target, "expiry") == "b"
    assert cursor(target, "discovery") == "a"
    target.expire_pending(hot_only=True, reconcile_seconds=60)
    target.run_once(hot_only=True, discover_only=True)
    assert cursor(target, "expiry") == "b"
    assert cursor(target, "discovery") == "a"
    replacement = scheduler(database)
    assert cursor(replacement, "expiry") is None
    assert cursor(replacement, "discovery") is None
    step(replacement, "expiry", size=100)
    assert completed(database, replacement, "expiry") == set(identifiers)


@pytest.mark.parametrize("mode", ["expiry", "discovery"])
def test_page_sql_budget_is_set_before_read_and_rechecked_after_read(
    database, monkeypatch, mode
):
    from v2_core import scheduling

    target = scheduler(database)
    scheduler_signal(database, "budget", expires=2)
    clock = [0.0]
    captured = []
    monkeypatch.setattr(scheduling.time, "perf_counter", lambda: clock[0])

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            captured.append((sql, args))
            result = self.conn.execute(sql, args)
            if sql.startswith("SELECT signal_id,request_key"):
                assert any(
                    "set_config('statement_timeout'" in item[0]
                    for item in captured[:-1]
                )
                clock[0] = 0.6
            return result

    @contextmanager
    def slow_page():
        with database() as conn:
            yield Connection(conn)

    target._connect = slow_page
    with pytest.raises(TimeoutError, match="HISTORY_PAGE_BUDGET_EXHAUSTED"):
        step(target, mode, budget_ms=500)
    assert cursor(target, mode) is None
    assert not any(sql.startswith("WITH ") for sql, _ in captured)
    assert completed(database, target, mode) == set()


def test_real_pg_timeout_rolls_back_then_adaptive_pages_finish_old_backlog(database):
    """Real statement cancellation after INSERT; no wall-clock speed assertion."""
    import threading

    from psycopg.types.json import Jsonb

    from services.v2_signal_maintenance import SignalMaintenance
    from v2_core.runtime_policy import resolve

    identifiers = [str(uuid4()) for _ in range(150)]
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_inbound_signals(signal_id,source,environment,request_key,snapshot) "
            "SELECT item.signal_id,'tv_bridge','SANDBOX',item.request_key,%s "
            "FROM unnest(%s::uuid[],%s::text[]) item(signal_id,request_key)",
            (
                Jsonb(
                    {
                        "observed_at": 1,
                        "expires_at_ms": 2,
                        "symbol": "ETHUSDT",
                        "signal": "TREND_UP",
                        "features": {},
                    }
                ),
                identifiers,
                [f"old-backlog-{number:04}" for number in range(150)],
            ),
        )
    target = scheduler(database, now=lambda: 1000000)
    stop, clock = threading.Event(), [0]
    maintenance = SignalMaintenance(
        [target],
        settings=lambda: resolve({"scheduler.history_page_size": 100}),
        stop=stop,
        monotonic=lambda: clock[0],
    )
    key, actual = maintenance.work[0]
    original = actual._connect
    injected = []
    attempted_pages = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=None):
            if sql.startswith("SELECT signal_id,request_key"):
                attempted_pages.append(args[-1])
            result = self.conn.execute(sql, args)
            if sql.startswith("INSERT INTO v2_signal_receipts") and not injected:
                # Prove real writes occurred before the induced query failure.
                count = self.conn.execute(
                    "SELECT count(*) FROM v2_signal_receipts WHERE consumer=%s",
                    (target.worker.scope.consumer,),
                ).fetchone()[0]
                assert count == 100
                injected.append(count)
                # Stop only this test's loop before the other lane can write;
                # inspect rollback and the unchanged cursor independently.
                stop.set()
                self.conn.execute("SET LOCAL statement_timeout='10ms'")
                self.conn.execute("SELECT pg_sleep(0.05)")
                pytest.fail("the real PostgreSQL timeout must interrupt this query")
            return result

    @contextmanager
    def first_dense_page_times_out():
        with original() as conn:
            yield Connection(conn)

    actual._connect = first_dense_page_times_out
    maintenance.run_once()
    assert injected == [100]
    assert attempted_pages == [100]
    assert cursor(actual, "expiry") is None
    assert actual._expiry_history_sweeps == 0
    assert completed(database, target, "expiry") == set()
    assert completed(database, target, "discovery") == set()
    failed = maintenance.snapshots[key]
    assert failed["component_failures"] == {"cold_expiry": "QueryCanceled"}
    assert failed["history"]["cold_expiry"]["effective_page_size"] == 50
    assert failed["history"]["cold_expiry"]["page_reductions"] == 1

    stop.clear()
    clock[0] = 5
    maintenance.run_once()
    recovered = maintenance.snapshots[key]
    assert recovered["status"] == "CURRENT"
    assert recovered["component_failures"] == {}
    assert recovered["history"]["cold_expiry"]["effective_page_size"] == 50
    assert recovered["history"]["cold_expiry"]["sweep_complete"] is True
    assert recovered["history"]["discovery"]["sweep_complete"] is True
    assert recovered["history"]["cold_expiry"]["scanned_total"] == 150
    assert recovered["progress"]["caught_up"] is None
    assert 50 in attempted_pages[1:]
    assert completed(database, target, "expiry") == set(identifiers)
    assert completed(database, target, "discovery") == set(identifiers)
    with database() as conn:
        assert conn.execute(
            "SELECT count(*),count(DISTINCT signal_id) FROM v2_signal_receipts "
            "WHERE consumer=%s AND outcome='EXPIRED'",
            (target.worker.scope.consumer,),
        ).fetchone() == (150, 150)
        assert conn.execute(
            "SELECT count(*),count(DISTINCT signal_id),count(*) FILTER(WHERE completed_at IS NOT NULL) "
            "FROM v2_strategy_tasks WHERE consumer=%s",
            (target.worker.scope.consumer,),
        ).fetchone() == (150, 150, 150)
        assert conn.execute(
            "SELECT count(*) FROM v2_strategy_decisions"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
