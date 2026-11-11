"""Auxiliary reads cannot permanently starve executable signal evaluation."""

from copy import copy

import pytest
from test_v2_trading_pipeline import case as pipeline_case

from tests.support.v2_admission import database as database_fixture
from v2_core.runtime_policy import PolicyStore

database = database_fixture
case = pipeline_case


def four_consumers(pipeline, database, monkeypatch):
    import services.v2_trading_pipeline as module

    tv = []
    for original in pipeline.schedulers:
        scheduler = copy(original)
        scheduler.worker = copy(original.worker)
        scheduler.worker.source = "tv_bridge"
        tv.append(scheduler)
    pipeline.schedulers += tuple(tv)
    PolicyStore(database, pipeline.scope).patch(
        {"scheduler.tv_first": True, "scheduler.evaluation_budget_ms": 16000},
        expected_version=0,
        reason="QA bounded advisory reads",
    )
    clock, processed = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    for scheduler in pipeline.schedulers:
        monkeypatch.setattr(scheduler, "progress", dict)
        monkeypatch.setattr(
            scheduler,
            "demand",
            lambda s=scheduler, **_: 1 if s.worker.source == "tv_bridge" else 16,
        )

        def run(s=scheduler, **kwargs):
            # Reproduces discovery spending more than the old ~470 ms TV
            # allowance. A run/call alone is not evidence of any processing.
            clock[0] += 0.6
            count = int(kwargs["budget_ms"] > 600)
            if count:
                processed.append((s.worker.scope.producer, s.worker.source))
                clock[0] += 0.1
            s.last_run_metrics = {"processed": count, "discovery_ms": 600}
            s.last_run_diagnostics = {"stage": "DONE" if count else "BUDGET_EXHAUSTED"}
            return {}

        monkeypatch.setattr(scheduler, "run_once", run)
    return clock, processed


def test_low_demand_tv_makes_real_progress_across_cycles(case, database, monkeypatch):
    pipeline, *_ = case
    _, processed = four_consumers(pipeline, database, monkeypatch)
    for _ in range(3):
        result = pipeline.run_once()
        for name in ("s6:tv_bridge", "s8:tv_bridge"):
            assert result["scheduler_allocation"][name]["processed"] == 1
            assert result["scheduler_allocation"][name]["work_status"] == "PROCESSED"
    assert processed.count(("s6", "tv_bridge")) == 3
    assert processed.count(("s8", "tv_bridge")) == 3


@pytest.mark.parametrize("slow_seconds", [4.1, 30.0])
def test_slow_demand_cannot_permanently_exhaust_evaluation(
    case, database, monkeypatch, slow_seconds
):
    pipeline, *_ = case
    clock, processed = four_consumers(pipeline, database, monkeypatch)
    probes = []
    for scheduler in pipeline.schedulers:

        def demand(**kwargs):
            assert kwargs["budget_ms"] <= 500 and kwargs["limit"] == 16
            probes.append(1)
            clock[0] += slow_seconds
            return 1

        monkeypatch.setattr(scheduler, "demand", demand)
    first = pipeline.run_once()
    second = pipeline.run_once()
    assert len(probes) == 1
    assert processed
    assert all(x["processed"] == 1 for x in second["scheduler_allocation"].values())
    if slow_seconds > 16:
        assert first["evaluation_stop_reason"] == "CYCLE_BUDGET_EXHAUSTED"


def test_demand_timeout_is_advisory_without_exposing_error_text(
    case, database, monkeypatch
):
    pipeline, *_ = case
    _, processed = four_consumers(pipeline, database, monkeypatch)

    def unavailable(**_):
        raise TimeoutError("private database details")

    for scheduler in pipeline.schedulers:
        monkeypatch.setattr(scheduler, "demand", unavailable)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE" and len(processed) == 4
    assert "private database" not in str(result)
    assert any(
        x["status"] == "UNAVAILABLE" for x in result["scheduler_demand"].values()
    )


def test_background_expiry_never_runs_in_foreground(case, database, monkeypatch):
    pipeline, *_ = case
    _, processed = four_consumers(pipeline, database, monkeypatch)

    class Background:
        def snapshot(self, _):
            return {"status": "UNAVAILABLE", "failure_type": "QueryCanceled"}

    pipeline.background_maintenance = Background()
    for scheduler in pipeline.schedulers:
        monkeypatch.setattr(
            scheduler, "expire_pending", lambda **_: pytest.fail("foreground expiry")
        )
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE" and len(processed) == 4
    assert all(
        x["status"] == "UNAVAILABLE"
        for x in result["phases"]["signal_maintenance"].values()
    )
    assert all(x["status"] == "DELEGATED" for x in result["expiry_metrics"].values())


def test_one_expiry_failure_is_not_global_entry_authority(case, database, monkeypatch):
    pipeline, *_ = case
    _, processed = four_consumers(pipeline, database, monkeypatch)

    def unavailable(**kwargs):
        assert 1 <= kwargs["budget_ms"] <= 500
        raise TimeoutError("private database details")

    monkeypatch.setattr(pipeline.schedulers[0], "expire_pending", unavailable)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE" and len(processed) == 4
    assert (
        result["phases"]["signal_maintenance"]["s6:s3"]["error_code"] == "TimeoutError"
    )
    assert "private database" not in str(result)


@pytest.mark.parametrize("stage", ["protection", "exits", "settlement"])
def test_safety_still_blocks_despite_advisory_isolation(
    case, database, monkeypatch, stage
):
    pipeline, *_ = case
    _, processed = four_consumers(pipeline, database, monkeypatch)
    monkeypatch.setattr(
        getattr(pipeline, stage), "run_once", lambda: {"status": "BLOCKED"}
    )
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED" and not processed
