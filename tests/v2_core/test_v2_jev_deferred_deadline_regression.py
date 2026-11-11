"""Existing task continuations retain freshness/risk checks and bounded work."""

import time
from types import SimpleNamespace

import pytest
from test_v2_signal_lag_classification import pending, report

from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_due, scheduler_signal, strategy_worker
from v2_core.scheduling import (
    ADMISSION_READ_QUOTA_CODES,
    JEV_CONTINUATION_CODES,
    JEV_MARKET_REFRESH_RETRY,
    JEV_PENDING_RETRY,
    JEV_QUOTA_RETRY,
    StrategyScheduler,
)
from v2_core.strategy import StrategyDecision

database = database_fixture


def seed_task(database, worker, signal_id, reason):
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,error_code) VALUES (%s,%s,%s)",
            (worker.scope.consumer, signal_id, reason),
        )


def no_orders(database):
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)


def test_refresh_wait_is_not_model_resume_priority_or_account_capacity():
    assert JEV_PENDING_RETRY in JEV_CONTINUATION_CODES
    assert JEV_QUOTA_RETRY in JEV_CONTINUATION_CODES
    assert JEV_MARKET_REFRESH_RETRY not in JEV_CONTINUATION_CODES
    assert JEV_QUOTA_RETRY in ADMISSION_READ_QUOTA_CODES
    assert JEV_PENDING_RETRY not in ADMISSION_READ_QUOTA_CODES


@pytest.mark.parametrize("reason", [JEV_PENDING_RETRY, JEV_MARKET_REFRESH_RETRY])
def test_pending_survives_restart_short_retry_without_same_run_spin(
    database, monkeypatch, reason
):
    signal_id = scheduler_signal(database, "pending-retry")
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "FRESH_REJECTION")
    )
    original_consume = worker.consume
    reads, waits = [], []

    def context(snapshot):
        reads.append(snapshot)
        return {}

    def wait(**_):
        waits.append(True)
        return False

    worker.entry_filter = SimpleNamespace(wait_pending=wait)
    monkeypatch.setattr(
        worker,
        "consume",
        lambda *_args, **_kwargs: {"status": "DEFERRED", "reason": reason},
    )
    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(limit=10, budget_ms=3000) == {signal_id: "DEFERRED"}
    assert len(reads) == 1
    assert len(waits) == int(reason == JEV_PENDING_RETRY)
    with database() as conn:
        code, attempts, token, complete, seconds = conn.execute(
            "SELECT error_code,attempts,lease_token,completed_at,"
            "extract(epoch FROM next_attempt_at-clock_timestamp()) "
            "FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone()
    assert (code, attempts, token, complete) == (reason, 1, None, None)
    assert 0 < seconds <= 1
    assert scheduler.last_run_diagnostics["jev_continuations"] == [
        {
            "signal_id": signal_id,
            "reason": reason,
            "original_cause": reason,
            "retry_seconds": 1,
            "execution_authorized": False,
        }
    ]
    no_orders(database)

    # A new scheduler has no private queue. The persisted task still has to
    # rebuild context and the real worker's fresh rejection wins over any hint.
    scheduler_due(database)
    monkeypatch.setattr(worker, "consume", original_consume)
    again = StrategyScheduler(worker, context_provider=context)
    assert again.run_once(limit=1) == {signal_id: "IGNORED"}
    assert len(reads) == 2
    with database() as conn:
        code, attempts, complete = conn.execute(
            "SELECT error_code,attempts,completed_at FROM v2_strategy_tasks "
            "WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone()
    assert code is None and attempts == 2 and complete is not None
    no_orders(database)


@pytest.mark.parametrize(
    ("marker", "expires", "prioritized"),
    [
        (JEV_PENDING_RETRY, 100, True),
        (JEV_QUOTA_RETRY, 100, True),
        (JEV_MARKET_REFRESH_RETRY, 100, False),
        ("UNRELATED_FAILURE", 100, False),
        (JEV_PENDING_RETRY, 5, False),
        (JEV_QUOTA_RETRY, 5, False),
    ],
)
def test_only_live_model_continuations_precede_new_candidates(
    database, marker, expires, prioritized
):
    ordinary = scheduler_signal(database, "ordinary", expires=50)
    continuation = scheduler_signal(database, "continuation", expires=expires)
    worker, _ = strategy_worker(
        database,
        now=lambda: 10,
        decide=lambda *_: StrategyDecision("IGNORED", "NO_AUTHORITY_FROM_PRIORITY"),
    )
    seed_task(database, worker, ordinary, None)
    seed_task(database, worker, continuation, marker)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    result = scheduler.run_once(limit=1)
    assert list(result) == [continuation if prioritized else ordinary]
    assert list(result.values()) == ["IGNORED"]
    no_orders(database)


def test_committed_recovery_stays_ahead_of_model_continuation(database):
    committed = scheduler_signal(database, "committed", expires=100)
    continuation = scheduler_signal(database, "continuation", expires=50)
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "FINAL")
    )
    assert worker.consume(committed, context={})["status"] == "IGNORED"
    seed_task(database, worker, committed, None)
    seed_task(database, worker, continuation, JEV_PENDING_RETRY)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    assert list(scheduler.run_once(limit=1)) == [committed]
    no_orders(database)


def test_quota_after_model_wake_retains_exact_cause_and_bounded_continuation(
    database, monkeypatch
):
    signal_id = scheduler_signal(database, "quota-after-wake")
    worker, _ = strategy_worker(database)
    reads = []

    def context(_):
        reads.append(True)
        if len(reads) >= 2:
            raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        return {}

    worker.entry_filter = SimpleNamespace(wait_pending=lambda **_: True)
    monkeypatch.setattr(
        worker,
        "consume",
        lambda *_args, **_kwargs: {
            "status": "DEFERRED",
            "reason": JEV_PENDING_RETRY,
        },
    )
    scheduler = StrategyScheduler(worker, context_provider=context)
    for iteration in range(2):
        if iteration:
            scheduler_due(database)
        assert scheduler.run_once(limit=20, budget_ms=3000) == {
            signal_id: "DEFERRED"
        }
        diagnostic = scheduler.last_run_diagnostics
        assert diagnostic["failures"][0]["error_code"] == "ADMISSION_BUDGET_UNAVAILABLE"
        assert diagnostic["candidate_waits"][0]["reason"] == "ADMISSION_CAPACITY_WAIT"
        assert diagnostic["candidate_waits"][0]["retry_seconds"] == 1
        assert diagnostic["jev_continuations"][0]["reason"] == JEV_QUOTA_RETRY
        with database() as conn:
            assert conn.execute(
                "SELECT error_code,attempts,lease_token,completed_at FROM v2_strategy_tasks "
                "WHERE consumer=%s AND signal_id=%s",
                (worker.scope.consumer, signal_id),
            ).fetchone() == (JEV_QUOTA_RETRY, iteration + 1, None, None)
        no_orders(database)
    assert len(reads) == 3  # first + recheck, then one fresh retry; never a loop


def test_transport_error_text_does_not_gain_quota_continuation(database):
    signal_id = scheduler_signal(database, "fake-quota")
    worker, _ = strategy_worker(database)
    seed_task(database, worker, signal_id, JEV_PENDING_RETRY)

    def failed_context(_):
        raise TimeoutError("ADMISSION_BUDGET_UNAVAILABLE")

    scheduler = StrategyScheduler(worker, context_provider=failed_context)
    assert scheduler.run_once(limit=1) == {signal_id: "UNAVAILABLE"}
    assert "jev_continuations" not in scheduler.last_run_diagnostics
    with database() as conn:
        assert conn.execute(
            "SELECT error_code FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s",
            (worker.scope.consumer, signal_id),
        ).fetchone() == ("TimeoutError",)
    no_orders(database)


@pytest.mark.parametrize(
    ("marker", "lag"),
    [(JEV_PENDING_RETRY, True), (JEV_MARKET_REFRESH_RETRY, True), (JEV_QUOTA_RETRY, False)],
)
def test_jev_task_diagnostics_preserve_lag_and_quota_categories(database, marker, lag):
    now = time.time_ns() // 1000000 + 31000
    pending(database, "jev-health", expiry=now + 60000, error=marker)
    _, saved = report(database, now)
    assert ("SIGNAL_CONSUMPTION_LAG" in saved["active"]) is lag
    detail = saved["queue_diagnostics"]["s6:tv_bridge"]
    assert detail["reason_codes"] == {marker: 1}
    assert detail["account_capacity_wait"] == 0
    assert detail["admission_read_quota_wait"] == int(marker == JEV_QUOTA_RETRY)
    no_orders(database)
