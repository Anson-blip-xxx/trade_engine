"""Same existing lease, real PG coordinator, synthetic market/fake SDK only."""

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_v2_jev_entry_context import binding, publish
from test_v2_jev_entry_context import case as memory_fixture
from test_v2_jev_fullflow import prepare_candidate
from test_v2_trading_pipeline import case as pipeline_fixture

from services import v2_jev_entry_context as subject
from tests.support.v2_admission import database as database_fixture
from v2_core.state import BusinessState

database = database_fixture
memory_case = memory_fixture
pipeline_case = pipeline_fixture


@pytest.mark.parametrize("limit", [50, 300, 2000])
def test_pg_poll_is_bounded_by_budget_and_model_timeout_without_transaction_sleep(
    memory_case, monkeypatch, limit
):
    case = memory_case
    assert case.service.assess(**case.args).status == "PENDING"
    _, original, _ = binding(case)
    elapsed = SimpleNamespace(seconds=0.0)

    def pause(seconds):
        assert case.db.active_statements is None
        elapsed.seconds += seconds
        case.db.now += max(1, round(seconds * 1000))

    monkeypatch.setattr(
        subject, "time", SimpleNamespace(monotonic=lambda: elapsed.seconds, sleep=pause)
    )
    assert (
        case.service.wait_pending(
            signal_id=case.args["signal_id"],
            consumer=case.args["consumer"],
            max_wait_ms=limit,
        )
        is False
    )
    assert 0 < elapsed.seconds <= min(limit, 1500) / 1000 + 0.000001
    assert binding(case)[1] == original
    assert len(case.queued) == 1


@pytest.mark.parametrize("finish", ["READY", "STOP", "EXPIRED", "LATE_PG"])
def test_pg_wait_ready_stop_expiry_and_late_results_are_only_bounded_hints(
    memory_case, monkeypatch, finish
):
    case = memory_case
    if finish == "EXPIRED":
        case.args["signal"]["expires_at_ms"] = case.db.now + 100
    assert case.service.assess(**case.args).status == "PENDING"
    _, original, _ = binding(case)
    timer = SimpleNamespace(seconds=0.0, stopped=False, published=False)

    def pause(seconds):
        assert case.db.active_statements is None
        timer.seconds += seconds
        case.db.now += max(1, round(seconds * 1000))
        if finish == "STOP":
            timer.stopped = True
        if finish in {"READY", "LATE_PG"} and not timer.published:
            publish(case)
            timer.published = True
            if finish == "LATE_PG":
                timer.seconds += 2

    monkeypatch.setattr(
        subject, "time", SimpleNamespace(monotonic=lambda: timer.seconds, sleep=pause)
    )
    assert case.service.wait_pending(
        signal_id=case.args["signal_id"],
        consumer=case.args["consumer"],
        max_wait_ms=2000,
        stop_requested=lambda: timer.stopped,
    ) is (finish == "READY")
    assert binding(case)[1] == original


def setup_scheduler(database, pipeline_case):
    candidate = prepare_candidate(database, pipeline_case)
    scheduler = pipeline_case[0].schedulers[0]
    scheduler.actionable_events = ("TREND_UP",)
    original_context = scheduler.context_provider
    reads = []

    def context(snapshot):
        reads.append(snapshot)
        now = candidate.clock.now()
        candidate.account.update(observed_at_ms=now, valid_until_ms=now + 60000)
        candidate.account["evidence"]["qa_refresh"] = len(reads)
        return original_context(snapshot)

    scheduler.context_provider = context
    return candidate, scheduler, reads, context


def no_orders(database):
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)


@pytest.mark.parametrize("outcome", ["PASS", "VETO", "FRESH_RISK", "ADMISSION_QUOTA"])
def test_actual_pg_ready_rechecks_fresh_risk_once_within_original_lease(
    database, pipeline_case, outcome
):
    candidate, scheduler, reads, original_context = setup_scheduler(
        database, pipeline_case
    )
    futures = []
    pool = ThreadPoolExecutor(max_workers=1)

    def sdk():
        time.sleep(0.03)  # Fake SDK in separate worker, not the scheduler thread.
        candidate.model(trend="0.1" if outcome == "VETO" else "0.8")

    def submit(_):
        if not futures:
            futures.append(pool.submit(sdk))

    candidate.worker.entry_filter.reference.submit = submit

    def fresh(snapshot):
        if len(reads) == 1:
            if outcome == "FRESH_RISK":
                candidate.account["sizing"]["available_margin"] = "0"
            if outcome == "ADMISSION_QUOTA":
                raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        return original_context(snapshot)

    scheduler.context_provider = fresh
    try:
        result = scheduler.run_once(limit=1, budget_ms=4000)
        for future in futures:
            future.result(timeout=5)
    finally:
        pool.shutdown(wait=True)
    expected = (
        "PREPARED"
        if outcome == "PASS"
        else "DEFERRED"
        if outcome == "ADMISSION_QUOTA"
        else "IGNORED"
    )
    assert result == {candidate.signal_id: expected}, result
    assert len(candidate.sdk_calls) == 1
    assert (
        BusinessState(database).read(candidate.binding) == candidate.immutable_binding
    )
    assert len(reads) == (1 if outcome == "ADMISSION_QUOTA" else 2)
    assert len(candidate.evaluations) == (2 if outcome == "ADMISSION_QUOTA" else 3)
    with database() as conn:
        attempts, token, completed = conn.execute(
            "SELECT attempts,lease_token,completed_at FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (candidate.worker.scope.consumer, candidate.signal_id),
        ).fetchone()
        assert attempts == 1 and token is None
        if outcome != "ADMISSION_QUOTA":
            assert completed is not None
    if outcome != "PASS":
        no_orders(database)
    else:
        assert scheduler.last_run_metrics["jev_recheck_count"] == 1
        with database() as conn:
            assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (
                1,
            )
            assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (1,)
        # Normal retry finds the immutable result; no second SDK or intent.
        assert scheduler.run_once(limit=1, budget_ms=4000) == {}
        assert len(candidate.sdk_calls) == 1


@pytest.mark.parametrize(
    "stop_at", ["WAIT", "FRESH_CONTEXT", "SCHEDULER_BUDGET", "LEASE_LOST"]
)
def test_stop_or_lost_budget_cannot_consume_after_wait(
    database, pipeline_case, stop_at
):
    candidate, scheduler, reads, original_context = setup_scheduler(
        database, pipeline_case
    )
    state = SimpleNamespace(stopped=False, monotonic=100.0)
    scheduler.monotonic = lambda: state.monotonic

    def ready(**_):
        if stop_at == "WAIT":
            state.stopped = True
        elif stop_at == "SCHEDULER_BUDGET":
            state.monotonic += 5
        elif stop_at == "LEASE_LOST":
            with database() as conn:
                conn.execute(
                    "UPDATE v2_strategy_tasks SET lease_token=NULL,lease_until=NULL WHERE consumer=%s AND signal_id=%s",
                    (candidate.worker.scope.consumer, candidate.signal_id),
                )
        return True

    candidate.worker.entry_filter.wait_pending = ready

    def context(snapshot):
        value = original_context(snapshot)
        if stop_at == "FRESH_CONTEXT" and len(reads) == 2:
            state.stopped = True
        return value

    scheduler.context_provider = context
    result = scheduler.run_once(
        limit=1, budget_ms=4000, stop_requested=lambda: state.stopped
    )
    assert result[candidate.signal_id] == ("DRAINED" if state.stopped else "DEFERRED")
    assert len(reads) == (2 if stop_at == "FRESH_CONTEXT" else 1)
    assert len(candidate.evaluations) == 2
    no_orders(database)


def test_pending_second_check_is_not_an_unbounded_recheck_loop(database, pipeline_case):
    candidate, scheduler, reads, _ = setup_scheduler(database, pipeline_case)
    called = []

    def wake(**_):
        called.append(True)
        return True  # Wake hint is not approval: PG still has no result.

    candidate.worker.entry_filter.wait_pending = wake
    assert scheduler.run_once(limit=1, budget_ms=4000) == {
        candidate.signal_id: "DEFERRED"
    }
    assert len(called) == 1 and len(reads) == 2
    assert len(candidate.evaluations) == 3
    no_orders(database)
