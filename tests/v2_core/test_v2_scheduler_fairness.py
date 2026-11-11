"""Fair consumer turns and durable failure evidence, never concurrent writers."""

from types import SimpleNamespace

import pytest
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_trading_pipeline import TradingPipeline
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal, strategy_worker
from v2_core.scheduling import StrategyScheduler
from v2_core.strategy import StrategyDecision

database = database_fixture
case = pipeline_case


def fake_consumers(pipeline, monkeypatch, *, first_error=False, overrun=False):
    import services.v2_trading_pipeline as module

    calls, clock = [], [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    for index, scheduler in enumerate(pipeline.schedulers):
        monkeypatch.setattr(scheduler, "demand", lambda **_: 1)
        monkeypatch.setattr(scheduler, "progress", dict)

        def run(scheduler=scheduler, index=index, **kwargs):
            calls.append(index)
            scheduler.last_run_metrics = {"processed": 0, "discovery_ms": 0.5}
            scheduler.last_run_diagnostics = {"stage": "DISCOVERY", "failures": []}
            if overrun and index == 0:
                clock[0] += 100
            if first_error and index == 0:
                raise TimeoutError("private connection string must not persist")
            return {}

        monkeypatch.setattr(scheduler, "run_once", run)
    return calls


def test_complete_pass_rotates_first_consumer(case, monkeypatch):
    pipeline, *_ = case
    calls = fake_consumers(pipeline, monkeypatch)
    for _ in range(3):
        result = pipeline.run_once()
        assert result["status"] == "CYCLE_COMPLETE"
        assert all(
            v["status"] == "FINISHED" for v in result["scheduler_allocation"].values()
        )
    assert calls == [0, 1, 1, 0, 0, 1]


def test_overrun_records_skipped_peer_and_gives_it_next_turn(case, monkeypatch):
    pipeline, *_ = case
    calls = fake_consumers(pipeline, monkeypatch, overrun=True)
    first = pipeline.run_once()
    assert calls == [0]
    assert first["evaluation_stop_reason"] == "CYCLE_BUDGET_EXHAUSTED"
    assert first["scheduler_allocation"]["s8:s3"] == {
        "status": "NOT_ATTEMPTED",
        "reason": "CYCLE_BUDGET_EXHAUSTED",
    }
    second = pipeline.run_once()
    assert calls == [0, 1, 0]
    assert second["scheduler_order"] == ["s8:s3", "s6:s3"]


def test_exception_yields_next_turn_but_stays_fail_closed(case, monkeypatch):
    pipeline, *_ = case
    calls = fake_consumers(pipeline, monkeypatch, first_error=True)
    first = pipeline.run_once()
    assert first["status"] == "ENTRY_BLOCKED"
    assert first["evaluation_stop_reason"] == "SAFETY_BLOCKED"
    assert first["scheduler_allocation"]["s6:s3"]["status"] == "FAILED"
    assert first["scheduler_diagnostics"]["s6:s3"]["stage"] == "DISCOVERY"
    assert first["scheduler_metrics"]["s6:s3"]["discovery_ms"] == 0
    assert "private connection" not in str(first)
    pipeline.run_once()
    assert calls == [0, 1, 0]


def test_blocked_safety_does_not_advance_turn(case, monkeypatch):
    pipeline, *_ = case
    calls = fake_consumers(pipeline, monkeypatch)
    monkeypatch.setattr(pipeline.protection, "run_once", lambda: {"status": "BLOCKED"})
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert not calls and pipeline._scheduler_heads == {}
    assert "scheduler_order" not in result


def test_busy_account_does_not_advance_turn(case, monkeypatch):
    from contextlib import contextmanager

    pipeline, *_ = case
    calls = fake_consumers(pipeline, monkeypatch)

    @contextmanager
    def occupied():
        yield False

    monkeypatch.setattr(pipeline, "_ownership", occupied)
    assert pipeline.run_once() == {"status": "BUSY"}
    assert not calls and pipeline._scheduler_heads == {}


def bare_pipeline():
    p = TradingPipeline.__new__(TradingPipeline)
    p._scheduler_heads = {}
    p.schedulers = tuple(
        SimpleNamespace(
            worker=SimpleNamespace(
                source=source,
                scope=SimpleNamespace(producer=producer, consumer=producer),
            )
        )
        for source in ("s3", "tv_bridge", "quant")
        for producer in ("s6", "s8")
    )
    return p


def identities(plan):
    return [s.worker.scope.producer + ":" + s.worker.source for _, s in plan]


def test_rotation_respects_tv_priority_and_quant_peers():
    p = bare_pipeline()
    p._scheduler_heads = {"tv": ("s8", "tv_bridge"), "other": ("s6", "quant")}
    assert identities(p._scheduler_groups(True)) == [
        "s8:tv_bridge",
        "s6:tv_bridge",
        "s6:quant",
        "s8:quant",
        "s6:s3",
        "s8:s3",
    ]
    assert identities(p._scheduler_groups(False)) == [
        "s6:s3",
        "s8:s3",
        "s6:tv_bridge",
        "s8:tv_bridge",
        "s6:quant",
        "s8:quant",
    ]
    p._scheduler_heads["all"] = ("s8", "quant")
    assert identities(p._scheduler_groups(False))[0] == "s8:quant"


def test_fairness_is_instance_local_and_unknown_member_is_safe():
    p, other = bare_pipeline(), bare_pipeline()
    p._scheduler_heads["other"] = ("s8", "s3")
    assert identities(other._scheduler_groups(True))[2] == "s6:s3"
    p._scheduler_heads["other"] = "removed-source"
    assert identities(p._scheduler_groups(True))[2] == "s6:s3"


def test_shared_direction_consumer_still_rotates_across_all_sources():
    p = bare_pipeline()
    seen = []
    for _ in range(7):
        plan = p._scheduler_groups(False)
        seen.append(identities(plan)[0])
        next_worker = plan[1][1].worker
        p._scheduler_heads["all"] = (next_worker.scope.consumer, next_worker.source)
    assert seen == [
        "s6:s3",
        "s8:s3",
        "s6:tv_bridge",
        "s8:tv_bridge",
        "s6:quant",
        "s8:quant",
        "s6:s3",
    ]


@pytest.mark.parametrize("tv_first", [True, False])
def test_actual_pipeline_rotates_shared_direction_ids_across_sources(
    case, database, monkeypatch, tv_first
):
    from copy import copy

    from v2_core.runtime_policy import PolicyStore

    pipeline, *_ = case
    tv = []
    for scheduler in pipeline.schedulers:
        duplicate = copy(scheduler)
        duplicate.worker = copy(scheduler.worker)
        duplicate.worker.source = "tv_bridge"
        tv.append(duplicate)
    pipeline.schedulers += tuple(tv)
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": tv_first},
        expected_version=0,
        reason="QA priority switch",
    )
    calls = fake_consumers(pipeline, monkeypatch)
    orders = []
    for _ in range(4):
        result = pipeline.run_once()
        assert result["status"] == "CYCLE_COMPLETE"
        orders.append(result["scheduler_order"])
    if tv_first:
        assert calls == [2, 3, 0, 1, 3, 2, 1, 0] * 2
    else:
        assert calls == [0, 1, 2, 3, 1, 2, 3, 0, 2, 3, 0, 1, 3, 0, 1, 2]
    assert all(len(set(order)) == 4 for order in orders)


@pytest.mark.parametrize("stage", ["READ", "CONTEXT_POLICY", "CONTEXT", "CONSUME"])
def test_attempt_failure_has_safe_stage_identity_and_retry_evidence(
    database, monkeypatch, stage
):
    sid = scheduler_signal(database, "failure-stage")
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})

    def fail(*args, **kwargs):
        raise TimeoutError("https://private-host/?token=not-to-be-saved")

    target, name = {
        "READ": (worker, "_read"),
        "CONTEXT_POLICY": (scheduler, "context_required"),
        "CONTEXT": (scheduler, "context_provider"),
        "CONSUME": (worker, "consume"),
    }[stage]
    monkeypatch.setattr(target, name, fail)
    assert scheduler.run_once(1) == {sid: "UNAVAILABLE"}
    assert scheduler.last_run_diagnostics["failures"] == [
        {
            "signal_id": sid,
            "attempt": 1,
            "stage": stage,
            "error_code": "TimeoutError",
            "outcome": "UNAVAILABLE",
        }
    ]
    assert "private-host" not in str(scheduler.last_run_diagnostics)
    with database() as c:
        assert c.execute(
            "SELECT completed_at,lease_token,error_code FROM v2_strategy_tasks WHERE signal_id=%s",
            (sid,),
        ).fetchone() == (None, None, "TimeoutError")
        assert c.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)


def test_slow_context_finishes_current_attempt_without_claiming_another(database):
    clock = [0.0]
    scheduler_signal(database, "slow-first")
    scheduler_signal(database, "slow-second")
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )

    def slow(_):
        clock[0] += 2
        return {}

    scheduler = StrategyScheduler(
        worker, context_provider=slow, monotonic=lambda: clock[0]
    )
    result = scheduler.run_once(2, budget_ms=1000)
    assert list(result.values()) == ["IGNORED"]
    assert scheduler.last_run_diagnostics["stop_reason"] == "BUDGET_EXHAUSTED"
    with database() as c:
        assert c.execute(
            "SELECT sum(attempts),count(*) FILTER (WHERE completed_at IS NOT NULL) FROM v2_strategy_tasks"
        ).fetchone() == (1, 1)
    assert len(scheduler.run_once(2, budget_ms=1000)) == 1


def test_empty_pass_resets_failure_diagnostics(database):
    worker, _ = strategy_worker(
        database, decide=lambda *_: StrategyDecision("IGNORED", "QA")
    )
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    scheduler.last_run_diagnostics = {"failures": [{"error_code": "OLD"}]}
    assert scheduler.run_once() == {}
    assert scheduler.last_run_diagnostics == {
        "queue_source": {
            "mode": "READY",
            "reason": "VALIDATED_TRANSACTIONAL_PROJECTION",
        },
        "failures": [],
        "stage": "COMPLETE",
        "stop_reason": "NO_DUE_TASK",
    }


def test_ack_failure_retains_lease_and_receipt_for_idempotent_recovery(
    database, monkeypatch
):
    from contextlib import contextmanager

    sid = scheduler_signal(database, "lost-ack")
    decisions = []

    def decide(*_):
        decisions.append(1)
        return StrategyDecision("IGNORED", "QA")

    worker, _ = strategy_worker(database, decide=decide)
    scheduler = StrategyScheduler(worker, context_provider=lambda _: {})
    original = scheduler._connect

    @contextmanager
    def fail_ack():
        with original() as conn:

            class Proxy:
                def execute(self, query, *args):
                    if query.startswith("UPDATE v2_strategy_tasks t SET"):
                        raise TimeoutError("private ACK endpoint")
                    return conn.execute(query, *args)

            yield Proxy()

    monkeypatch.setattr(scheduler, "_connect", fail_ack)
    assert scheduler.run_once(1) == {sid: "UNAVAILABLE"}
    assert scheduler.last_run_diagnostics["failures"] == [
        {
            "signal_id": sid,
            "attempt": 1,
            "stage": "ACK",
            "error_code": "TimeoutError",
            "outcome": "UNAVAILABLE",
        }
    ]
    with database() as c:
        assert c.execute(
            "SELECT completed_at IS NULL,lease_token IS NOT NULL FROM v2_strategy_tasks WHERE signal_id=%s",
            (sid,),
        ).fetchone() == (True, True)
        assert c.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s", (sid,)
        ).fetchone() == (1,)
        # QA-only expiry of this test's lease, not a runtime recovery shortcut.
        c.execute(
            "UPDATE v2_strategy_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE signal_id=%s",
            (sid,),
        )
    monkeypatch.setattr(scheduler, "_connect", original)
    assert scheduler.run_once(1) == {sid: "IGNORED"}
    assert decisions == [1]
    assert scheduler.last_run_diagnostics["failures"] == []


def test_pipeline_diagnostics_are_durable_even_when_scheduler_raises(
    case, database, monkeypatch
):
    pipeline, *_ = case
    fake_consumers(pipeline, monkeypatch, first_error=True)
    first = pipeline.run_once()
    pipeline.run_once()
    with database() as c:
        row = c.execute(
            "SELECT payload FROM v2_business_state WHERE scope->>'namespace'='trading-pipeline-cycle-v1' AND scope->>'key'=%s",
            (first["cycle_id"],),
        ).fetchone()
    assert row[0]["scheduler_diagnostics"] == first["scheduler_diagnostics"]
    assert row[0]["scheduler_allocation"] == first["scheduler_allocation"]
    assert row[0]["phases"]["s6"]["error_code"] == "TimeoutError"
