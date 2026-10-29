"""Diagnostics must not change the scheduling clock, leases or business results."""

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import scheduler_signal, strategy_worker

from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture


@pytest.mark.parametrize("mode", ["ok", "context-error", "consume-error"])
def test_metrics_cover_success_and_failure_without_exposing_context(database, mode):
    sid = scheduler_signal(database, "metric")

    def context(_):
        if mode == "context-error":
            raise ValueError("QA_CONTEXT_ERROR")
        return {"private-data": "must-not-be-in-metrics"}

    def decide(*_):
        if mode == "consume-error":
            raise ValueError("QA_CONSUME_ERROR")
        return StrategyDecision("IGNORED", "QA")

    worker, _ = strategy_worker(database, decide=decide)
    scheduler = StrategyScheduler(worker, context_provider=context)
    assert scheduler.run_once(1)[sid] == ("IGNORED" if mode == "ok" else "UNAVAILABLE")
    metrics = scheduler.last_run_metrics
    assert set(metrics) == {
        "discovery_ms",
        "claim_ms",
        "context_ms",
        "consume_ms",
        "ack_ms",
        "elapsed_ms",
        "processed",
    }
    assert metrics["processed"] == 1
    assert all(type(value) is int and value >= 0 for value in metrics.values())
    assert "must-not-be-in-metrics" not in str(metrics)
    assert metrics["elapsed_ms"] >= max(
        value for key, value in metrics.items() if key.endswith("_ms")
    )
    with database() as c:
        assert c.execute(
            "SELECT count(*) FROM v2_strategy_tasks WHERE lease_token IS NOT NULL"
        ).fetchone() == (0,)


def test_metrics_reset_for_an_empty_cycle(database):
    scheduler_signal(database, "metric-empty")
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    scheduler.run_once(1)
    assert scheduler.last_run_metrics["processed"] == 1
    assert scheduler.run_once(1) == {}
    assert scheduler.last_run_metrics["processed"] == 0
    assert scheduler.last_run_metrics["context_ms"] == 0
