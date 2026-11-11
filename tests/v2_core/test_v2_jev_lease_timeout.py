"""Real isolated-PG lease-read timeouts are hints, never entry authority.

Only the scheduler's auxiliary read connection is proxied. Worker/admission
transactions and the JEV binding/result use the real isolated database; no
exchange transport or production configuration is used.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from psycopg.errors import LockNotAvailable, QueryCanceled
from test_v2_jev_same_cycle import no_orders, setup_scheduler
from test_v2_signal_context_isolation import tv_schedulers, tv_signal
from test_v2_trading_pipeline import case as pipeline_fixture

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_due, scheduler_signal, strategy_worker
from v2_core.runtime_policy import PolicyStore
from v2_core.scheduling import JEV_PENDING_RETRY, StrategyScheduler
from v2_core.state import BusinessState
from v2_core.strategy import StrategyDecision

database = database_fixture
pipeline_case = pipeline_fixture
PHASES = ("BEFORE_WAIT", "AFTER_WAIT", "AFTER_NEGATIVE_CHECK", "AFTER_CONTEXT")
LEASE_SELECT = "SELECT greatest(0,extract(epoch FROM (lease_until-clock_timestamp()))"


def inject_lease_read(database, scheduler, phase, action=None):
    """Inject once at the actual auxiliary SQL under its real 100 ms timeout."""
    state = SimpleNamespace(checks=0, failures=0, read_only=[])

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, params=None):
            if isinstance(query, str) and query.startswith(LEASE_SELECT):
                state.checks += 1
                state.read_only.append(
                    self.conn.execute("SHOW transaction_read_only").fetchone()[0]
                )
                assert state.read_only[-1] == "on"
                if state.checks == PHASES.index(phase) + 1:
                    if action is not None:
                        action(self.conn, params)
                    else:
                        try:
                            self.conn.execute("SELECT pg_sleep(0.5)")
                        except QueryCanceled:
                            state.failures += 1
                            raise
                        pytest.fail("the real 100 ms PG statement timeout did not fire")
            return self.conn.execute(query, params)

    @contextmanager
    def connect():
        with database() as conn:
            yield Connection(conn)

    scheduler._connect = connect
    return state


def task(database, candidate):
    with database() as conn:
        return conn.execute(
            "SELECT error_code,attempts,lease_token,completed_at,"
            "extract(epoch FROM next_attempt_at-clock_timestamp()) "
            "FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (candidate.worker.scope.consumer, candidate.signal_id),
        ).fetchone()


def same_unclaimed_request(candidate):
    # The fixture's append-only submit does not claim the provider request.
    # Setup and initial consume can each enqueue the SAME immutable request;
    # the real PG coordinator, not enqueue count, deduplicates SDK execution.
    bound = json.loads(candidate.immutable_binding.payload_json)
    assert candidate.queued == [bound["public_features"], bound["public_features"]]


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("fresh_risk", [False, True])
def test_actual_pg_timeout_defers_once_and_reuses_original_model_on_fresh_retry(
    database, pipeline_case, phase, fresh_risk
):
    candidate, scheduler, reads, _ = setup_scheduler(database, pipeline_case)
    waits = []

    def ready(**_):
        waits.append(True)
        candidate.model()
        return True

    candidate.worker.entry_filter.wait_pending = ready
    injected = inject_lease_read(database, scheduler, phase)
    assert scheduler.run_once(limit=10, budget_ms=4000) == {
        candidate.signal_id: "DEFERRED"
    }
    assert injected.failures == 1
    assert injected.checks == PHASES.index(phase) + 1
    assert len(waits) == int(phase != "BEFORE_WAIT")
    assert len(reads) == (2 if phase == "AFTER_CONTEXT" else 1)
    assert len(candidate.evaluations) == 2  # setup + initial consume, no re-consume
    same_unclaimed_request(candidate)
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )
    code, attempts, token, completed, retry_seconds = task(database, candidate)
    assert (code, attempts, token, completed) == (JEV_PENDING_RETRY, 1, None, None)
    assert 0 < retry_seconds <= 1
    diagnostic = scheduler.last_run_diagnostics
    review = diagnostic["jev_same_cycle"][0]
    assert review["reason"] == "JEV_LEASE_CHECK_TIMEOUT"
    assert review["lease_check_error"] == "QueryCanceled"
    assert review["lease_check_phase"] == phase
    assert review["execution_authorized"] is False
    assert review["rechecked"] is False
    assert diagnostic["failures"] == []
    assert diagnostic["jev_continuations"][0]["reason"] == JEV_PENDING_RETRY
    assert scheduler.last_run_metrics["jev_lease_read_timeout_count"] == 1
    no_orders(database)

    # A new attempt is due explicitly; the test does not sleep or extend the
    # model/signal deadline. The worker must still rebuild risk before admission.
    scheduler_due(database)
    if not candidate.sdk_calls:
        candidate.model()
    if fresh_risk:
        candidate.account["sizing"]["available_margin"] = "0"
    expected = "IGNORED" if fresh_risk else "PREPARED"
    assert scheduler.run_once(limit=1, budget_ms=4000) == {
        candidate.signal_id: expected
    }
    assert len(candidate.sdk_calls) == 1
    same_unclaimed_request(candidate)
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )
    if fresh_risk:
        no_orders(database)
    else:
        with database() as conn:
            assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (
                1,
            )
            assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)


def simple_pending(database):
    pending = scheduler_signal(database, "lease-timeout", expires=9)
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA_FRESH_REJECTION")
    )
    actual_consume = worker.consume
    worker.consume = lambda signal_id, **kwargs: (
        {"status": "DEFERRED", "reason": JEV_PENDING_RETRY}
        if signal_id == pending
        else actual_consume(signal_id, **kwargs)
    )
    worker.entry_filter = SimpleNamespace(wait_pending=lambda **_: True)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    return pending, worker, scheduler


def test_auxiliary_timeout_does_not_poison_next_candidate_or_spin(database):
    pending, worker, scheduler = simple_pending(database)
    unrelated = scheduler_signal(database, "unrelated-next-candidate", expires=10)
    injected = inject_lease_read(database, scheduler, "BEFORE_WAIT")
    assert scheduler.run_once(limit=20, budget_ms=4000) == {
        pending: "DEFERRED",
        unrelated: "IGNORED",
    }
    assert injected.checks == injected.failures == 1
    assert scheduler.last_run_diagnostics["failures"] == []
    with database() as conn:
        assert conn.execute(
            "SELECT attempts,error_code,completed_at IS NULL FROM v2_strategy_tasks "
            "WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, pending),
        ).fetchone() == (1, JEV_PENDING_RETRY, True)
    no_orders(database)


def test_full_pipeline_read_timeout_keeps_safety_first_and_other_source_running(
    database, pipeline_case
):
    pipeline, calls, _, _, now = pipeline_case
    television = tv_schedulers(pipeline, now, lambda _: {})
    pipeline.schedulers += television
    identifier = tv_signal(pipeline, now)
    scheduler = television[0]

    def pending(*_args, **_kwargs):
        calls.append("tv_pending")
        return {"status": "DEFERRED", "reason": JEV_PENDING_RETRY}

    scheduler.worker.consume = pending
    scheduler.worker.entry_filter = SimpleNamespace(
        wait_pending=lambda **_: pytest.fail("unknown lease must not poll the model")
    )
    inject_lease_read(database, scheduler, "BEFORE_WAIT")
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": True, "scheduler.evaluation_budget_ms": 16000},
        expected_version=0,
        reason="QA optional JEV lease read must not block a separate source",
    )
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert calls[:4] == ["protection", "exits", "settlement", "followups"]
    assert calls.index("settlement") < calls.index("tv_pending")
    assert calls.count("tv_pending") == 1
    assert result["phases"]["s6:tv"][identifier] == "DEFERRED"
    assert any(status == "PREPARED" for status in result["phases"]["s6"].values())
    assert result["scheduler_allocation"]["s6:s3"]["status"] == "FINISHED"
    assert result.get("evaluation_stop_reason") != "SAFETY_BLOCKED"
    diagnostic = result["scheduler_diagnostics"]["s6:tv_bridge"]
    assert diagnostic["failures"] == []
    assert diagnostic["jev_same_cycle"][0]["reason"] == "JEV_LEASE_CHECK_TIMEOUT"
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_trade_intents WHERE signal_id=%s", (identifier,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM v2_orders WHERE status='PREPARED'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "stage", ["CONTEXT", "FRESH_CONTEXT", "CONSUME", "RECONSUME", "WAIT", "ACK"]
)
def test_same_pg_error_outside_auxiliary_read_remains_unavailable(database, stage):
    pending, worker, scheduler = simple_pending(database)
    reads, consumes = [], []
    actual_consume = worker.consume

    def timeout():
        with database() as conn:
            conn.execute("SET LOCAL statement_timeout='20ms'")
            conn.execute("SELECT pg_sleep(0.2)")
        pytest.fail("isolated PostgreSQL should cancel this statement")

    def context(_):
        reads.append(True)
        if stage == "CONTEXT" or (stage == "FRESH_CONTEXT" and len(reads) == 2):
            timeout()
        return {}

    def consume(*args, **kwargs):
        consumes.append(True)
        if stage == "CONSUME" or (stage == "RECONSUME" and len(consumes) == 2):
            timeout()
        return actual_consume(*args, **kwargs)

    scheduler.context_provider = context
    worker.consume = consume
    if stage == "WAIT":
        worker.entry_filter.wait_pending = lambda **_: timeout()
    if stage == "ACK":

        class AckConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, query, params=None):
                if isinstance(query, str) and query.startswith(
                    "UPDATE v2_strategy_tasks t SET"
                ):
                    timeout()
                return self.conn.execute(query, params)

        @contextmanager
        def connect():
            with database() as conn:
                yield AckConnection(conn)

        scheduler._connect = connect
    assert scheduler.run_once(limit=1, budget_ms=4000) == {pending: "UNAVAILABLE"}
    failures = scheduler.last_run_diagnostics["failures"]
    assert len(failures) == 1
    assert failures[0]["error_code"] == "QueryCanceled"
    assert failures[0]["outcome"] == "UNAVAILABLE"
    assert not scheduler.last_run_metrics.get("jev_lease_read_timeout_count")
    no_orders(database)


@pytest.mark.parametrize("phase", PHASES)
def test_lease_loss_never_consumes_even_when_model_has_finished(
    database, pipeline_case, phase
):
    candidate, scheduler, _, _ = setup_scheduler(database, pipeline_case)

    def ready(**_):
        candidate.model()
        return True

    candidate.worker.entry_filter.wait_pending = ready

    def lose(_conn, params):
        with database() as conn:
            conn.execute(
                "UPDATE v2_strategy_tasks SET lease_token=NULL,lease_until=NULL "
                "WHERE consumer=%s AND signal_id=%s AND lease_token=%s",
                params,
            )

    inject_lease_read(database, scheduler, phase, lose)
    assert scheduler.run_once(limit=1, budget_ms=4000) == {
        candidate.signal_id: "DEFERRED"
    }
    assert len(candidate.evaluations) == 2
    assert scheduler.last_run_diagnostics["jev_same_cycle"][0]["rechecked"] is False
    assert not scheduler.last_run_metrics.get("jev_lease_read_timeout_count")
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )
    no_orders(database)


@pytest.mark.parametrize("expired_boundary", ["SIGNAL", "REVIEW"])
def test_original_deadline_expiry_after_read_timeout_cannot_be_refreshed(
    database, pipeline_case, expired_boundary
):
    candidate, scheduler, _, _ = setup_scheduler(database, pipeline_case)
    inject_lease_read(database, scheduler, "BEFORE_WAIT")
    assert scheduler.run_once(limit=1, budget_ms=4000) == {
        candidate.signal_id: "DEFERRED"
    }
    candidate.model()
    with database() as conn:
        signal_expiry = conn.execute(
            "SELECT (snapshot->>'expires_at_ms')::bigint FROM v2_inbound_signals "
            "WHERE signal_id=%s",
            (candidate.signal_id,),
        ).fetchone()[0]
    bound = json.loads(candidate.immutable_binding.payload_json)
    if expired_boundary == "SIGNAL":
        target = signal_expiry + 1
        expected, reason = "EXPIRED", "signal expired before evaluation"
    else:
        target = bound["deadline_ms"] + 1
        assert target < signal_expiry
        expected, reason = "IGNORED", "JEV_REVIEW_DEADLINE_EXPIRED"
        original_connect = candidate.worker.entry_filter.connect

        class ClockConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def execute(self, query, params=None):
                # The gate's authoritative clock normally comes from PG, not
                # runtime.clock_ms. Advance only this clock result with the
                # synthetic runtime time; all bindings/results/receipts still
                # use actual PG. Neither immutable deadline is rewritten.
                if (
                    query
                    == "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint"
                ):
                    return SimpleNamespace(fetchone=lambda: (candidate.clock.now(),))
                return self.conn.execute(query, params)

        @contextmanager
        def future_clock():
            with original_connect() as conn:
                yield ClockConnection(conn)

        candidate.worker.entry_filter.connect = future_clock
    candidate.clock.offset += target - candidate.clock.now()
    scheduler_due(database)
    assert scheduler.run_once(limit=1, budget_ms=4000) == {
        candidate.signal_id: expected
    }
    with database() as conn:
        assert conn.execute(
            "SELECT outcome,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (candidate.worker.scope.consumer, candidate.signal_id),
        ).fetchone() == (expected, reason)
    assert len(candidate.sdk_calls) == 1
    same_unclaimed_request(candidate)
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )
    no_orders(database)


@pytest.mark.parametrize("error", [LockNotAvailable, RuntimeError])
def test_only_typed_lock_timeout_gains_auxiliary_read_deferral(database, error):
    pending, _, scheduler = simple_pending(database)

    def fail(_conn, _params):
        raise error("isolated QA failure")

    inject_lease_read(database, scheduler, "BEFORE_WAIT", fail)
    expected = "DEFERRED" if error is LockNotAvailable else "UNAVAILABLE"
    assert scheduler.run_once(limit=1, budget_ms=4000) == {pending: expected}
    if error is LockNotAvailable:
        assert (
            scheduler.last_run_diagnostics["jev_same_cycle"][0]["lease_check_error"]
            == "LockNotAvailable"
        )
    else:
        assert (
            scheduler.last_run_diagnostics["failures"][0]["error_code"]
            == "RuntimeError"
        )
    no_orders(database)


def test_actual_pg_lock_timeout_is_only_a_pending_read_hint(database):
    pending, _, scheduler = simple_pending(database)

    def hold_table_then_read(conn, _params):
        with database() as blocker:
            blocker.execute("LOCK TABLE v2_strategy_tasks IN ACCESS EXCLUSIVE MODE")
            # A real table read has to wait for this isolated transaction;
            # lock_timeout=50ms wins before statement_timeout=100ms.
            conn.execute("SELECT 1 FROM v2_strategy_tasks LIMIT 1")
        pytest.fail("the isolated PostgreSQL lock timeout should have fired")

    inject_lease_read(database, scheduler, "BEFORE_WAIT", hold_table_then_read)
    assert scheduler.run_once(limit=1, budget_ms=4000) == {pending: "DEFERRED"}
    review = scheduler.last_run_diagnostics["jev_same_cycle"][0]
    assert review["lease_check_error"] == "LockNotAvailable"
    assert review["execution_authorized"] is False
    no_orders(database)


def test_drain_during_timed_out_read_still_dominates_retry(database):
    pending, _, scheduler = simple_pending(database)
    stopped = SimpleNamespace(value=False)

    def stop_then_timeout(conn, _params):
        stopped.value = True
        conn.execute("SELECT pg_sleep(0.5)")

    inject_lease_read(database, scheduler, "BEFORE_WAIT", stop_then_timeout)
    assert scheduler.run_once(
        limit=20, budget_ms=4000, stop_requested=lambda: stopped.value
    ) == {pending: "DRAINED"}
    diagnostic = scheduler.last_run_diagnostics
    assert diagnostic["stop_reason"] == "STOP_REQUESTED"
    assert "jev_continuations" not in diagnostic
    assert diagnostic["failures"] == []
    no_orders(database)


@pytest.mark.parametrize("phase", PHASES)
def test_successful_sql_that_exhausts_scheduler_budget_never_reconsumes(
    database, phase
):
    pending, worker, scheduler = simple_pending(database)
    clock = SimpleNamespace(now=100.0)
    scheduler.monotonic = lambda: clock.now
    consumes = []
    actual_consume = worker.consume

    def consume(*args, **kwargs):
        consumes.append(True)
        return actual_consume(*args, **kwargs)

    worker.consume = consume

    def slow_read(_conn, _params):
        clock.now += 4.1

    inject_lease_read(database, scheduler, phase, slow_read)
    assert scheduler.run_once(limit=20, budget_ms=4000) == {pending: "DEFERRED"}
    assert len(consumes) == 1
    assert scheduler.last_run_diagnostics["jev_same_cycle"][0]["rechecked"] is False
    assert not scheduler.last_run_metrics.get("jev_lease_read_timeout_count")
    no_orders(database)
