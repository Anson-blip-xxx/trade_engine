"""Bounded advisory counts and optional expiry budgets on isolated PostgreSQL."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal, strategy_worker

from v2_core.scheduling import StrategyScheduler
from v2_core.signals import Signals
from v2_core.strategy import StrategyDecision

database = database_fixture


def scheduler(database):
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    return StrategyScheduler(
        worker, context_provider=lambda _: {}, priority_events=("TREND_UP",)
    )


def no_database_scheduler():
    def forbidden():
        pytest.fail("invalid bounds must be rejected before connecting")

    return StrategyScheduler(
        SimpleNamespace(_connect=forbidden), context_provider=lambda _: {}
    )


@pytest.mark.parametrize("budget_ms", [0, -1, 5001, True, 1.5, "100", None])
def test_demand_rejects_invalid_budget_before_connecting(budget_ms):
    with pytest.raises(ValueError, match="DEMAND_QUERY_BUDGET_INVALID"):
        no_database_scheduler().demand(budget_ms=budget_ms)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "10", None])
def test_demand_rejects_invalid_limit_before_connecting(limit):
    with pytest.raises(ValueError, match="DEMAND_QUERY_LIMIT_INVALID"):
        no_database_scheduler().demand(limit=limit)


@pytest.mark.parametrize("budget_ms", [0, -1, 5001, True, 1.5, "100"])
def test_expiry_rejects_invalid_budget_before_connecting(budget_ms):
    with pytest.raises(ValueError, match="EXPIRY_QUERY_BUDGET_INVALID"):
        no_database_scheduler().expire_pending(budget_ms=budget_ms)


def queue_snapshot(database):
    with database() as conn:
        return [
            conn.execute(query).fetchall()
            for query in (
                "SELECT signal_id,snapshot FROM v2_inbound_signals ORDER BY signal_id",
                (
                    "SELECT consumer,signal_id,attempts,lease_token,lease_until,"
                    "next_attempt_at,completed_at FROM v2_strategy_tasks "
                    "ORDER BY consumer,signal_id"
                ),
                (
                    "SELECT consumer,signal_id,outcome FROM v2_signal_receipts "
                    "ORDER BY consumer,signal_id"
                ),
                "SELECT decision_id FROM v2_strategy_decisions ORDER BY decision_id",
                "SELECT order_id FROM v2_orders ORDER BY order_id",
            )
        ]


def test_capped_demand_preserves_scope_filters_and_queue_state(database):
    target = scheduler(database)
    worker = target.worker
    ready = [scheduler_signal(database, f"ready-{i}") for i in range(6)]
    scheduler_signal(database, "expired", expires=3)  # Equality is expired.
    scheduler_signal(database, "future", observed=5)
    done = scheduler_signal(database, "receipted")
    worker.consume(done, context={})
    retry = scheduler_signal(database, "backoff")
    leased = scheduler_signal(database, "leased")
    completed = scheduler_signal(database, "completed")
    other, _ = strategy_worker(database, producer="s8")
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason) "
            "VALUES(%s,%s,'IGNORED','OTHER_CONSUMER')",
            (other.scope.consumer, ready[0]),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,next_attempt_at) "
            "VALUES(%s,%s,clock_timestamp()+interval '1 minute')",
            (worker.scope.consumer, retry),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,lease_token,lease_until) "
            "VALUES(%s,%s,'00000000-0000-0000-0000-000000000001',"
            "clock_timestamp()+interval '1 minute')",
            (worker.scope.consumer, leased),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at) "
            "VALUES(%s,%s,clock_timestamp())",
            (worker.scope.consumer, completed),
        )
    for source, environment, event in (
        ("s3", "SANDBOX", "TREND_UP"),
        ("tv_bridge", "LIVE", "TREND_UP"),
        ("tv_bridge", "SANDBOX", "HIGH_VOL"),
    ):
        Signals(database).admit(
            source=source,
            environment=environment,
            request_key=f"noise-{source}-{environment}-{event}",
            snapshot={
                "observed_at": 1,
                "expires_at_ms": 10,
                "symbol": "ETHUSDT",
                "signal": event,
                "features": {},
            },
        )

    @contextmanager
    def read_only():
        with database() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            yield conn

    target._connect = read_only
    before = queue_snapshot(database)
    assert target.demand(limit=3) == 3
    assert target.demand(limit=10) == 6
    assert target.demand() == 6  # Existing no-argument callers remain valid.
    assert queue_snapshot(database) == before


@pytest.mark.parametrize("method", ["demand", "expire_pending"])
@pytest.mark.parametrize("budget_ms", [75, 1000])
def test_budget_settings_apply_to_real_transaction(database, method, budget_ms):
    target = scheduler(database)
    observed = []

    @contextmanager
    def capture_settings():
        with database() as conn:
            yield conn
            observed.append(
                conn.execute(
                    "SELECT current_setting('statement_timeout'),"
                    "current_setting('lock_timeout')"
                ).fetchone()
            )

    target._connect = capture_settings
    assert getattr(target, method)(budget_ms=budget_ms) == 0
    assert observed == [
        (f"{budget_ms}ms" if budget_ms != 1000 else "1s", f"{min(budget_ms, 250)}ms")
    ]


@pytest.mark.parametrize("kwargs", [{}, {"budget_ms": None}])
def test_expiry_without_budget_preserves_caller_limits_and_receipt(database, kwargs):
    target = scheduler(database)
    expired = scheduler_signal(database, "old", expires=2)
    observed = []

    @contextmanager
    def configured():
        with database() as conn:
            conn.execute("SET LOCAL statement_timeout='4321ms'")
            conn.execute("SET LOCAL lock_timeout='321ms'")
            yield conn
            observed.append(
                conn.execute(
                    "SELECT current_setting('statement_timeout'),"
                    "current_setting('lock_timeout')"
                ).fetchone()
            )

    target._connect = configured
    assert target.expire_pending(**kwargs) == 1
    assert observed == [("4321ms", "321ms")]
    with database() as conn:
        assert conn.execute(
            "SELECT outcome FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (target.worker.scope.consumer, expired),
        ).fetchone() == ("EXPIRED",)


def test_demand_statement_timeout_propagates_for_pipeline_fallback(database):
    from psycopg.errors import QueryCanceled

    target = scheduler(database)

    @contextmanager
    def slow_query():
        with database() as conn:

            class DelayedCount:
                def execute(self, query, params=None):
                    if query.startswith("SELECT count(*) FROM ("):
                        query = (
                            "WITH delay AS MATERIALIZED (SELECT pg_sleep(1)) "
                            "SELECT demand.* FROM delay CROSS JOIN ("
                            + query
                            + ") demand"
                        )
                    return conn.execute(query, params)

            yield DelayedCount()

    target._connect = slow_query
    with pytest.raises(QueryCanceled):
        target.demand(budget_ms=25)


def test_demand_lock_timeout_propagates_without_queue_mutation(database):
    from psycopg.errors import LockNotAvailable

    target = scheduler(database)
    scheduler_signal(database, "ready")
    before = queue_snapshot(database)
    with database() as holder:
        holder.execute("LOCK TABLE v2_inbound_signals IN ACCESS EXCLUSIVE MODE")
        with pytest.raises(LockNotAvailable):
            target.demand(budget_ms=2000)
    assert queue_snapshot(database) == before
