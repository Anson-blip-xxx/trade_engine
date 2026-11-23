"""Pure orchestration QA: no database, network, venue or trading authority."""

from collections import Counter
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

import pytest

import services.v2_trading_pipeline as module
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import resolve


@pytest.fixture
def harness(monkeypatch):
    clock, calls, saved, prepared, dispatches, save_hooks = [0.0], [], [], [], [], []
    settings = resolve(
        {
            "scheduler.evaluation_budget_ms": 16000,
            "scheduler.signal_batch": 100,
            "scheduler.tv_first": True,
        }
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])

    class Store:
        def __init__(self, _):
            pass

        def change(self, _key, **kwargs):
            saved.append(deepcopy(kwargs["payload"]))
            for hook in save_hooks:
                hook(kwargs["payload"])
            return SimpleNamespace(code="APPLIED")

    monkeypatch.setattr(module, "BusinessState", Store)
    monkeypatch.setattr(
        module,
        "PolicyStore",
        lambda *_: SimpleNamespace(
            read=lambda: SimpleNamespace(values=settings, version=0, digest="qa")
        ),
    )

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *_: SimpleNamespace(
                fetchall=lambda: [(item,) for item in prepared]
            )
        )

    scope = AccountScope("BINANCE", "qa-reclaim", "SANDBOX", "FUTURES")
    pipeline = module.TradingPipeline.__new__(module.TradingPipeline)
    pipeline.scope, pipeline.connect = scope, connect
    pipeline._ownership = lambda: nullcontext(True)
    pipeline._scheduler_heads, pipeline._progress_cache = {}, {}
    pipeline._demand_backoff_until = 0.0
    pipeline.enable_entries = False
    pipeline.background_maintenance = SimpleNamespace(
        snapshot=lambda _: {"status": "CURRENT"}
    )

    def dispatch(order):
        dispatches.append(order)
        prepared.remove(order)
        return "FILLED"

    pipeline.runtime = SimpleNamespace(
        _now=lambda: 1000000 + round(clock[0] * 1000),
        tick=lambda **_: {},
        execution=SimpleNamespace(dispatch=dispatch, recover=lambda _: "FILLED"),
    )
    pipeline.market = SimpleNamespace(run_once=lambda: {"status": "CURRENT"})
    pipeline.regime = SimpleNamespace(run_once=lambda: {"status": "CLEAR"})
    for name in ("protection", "exits", "settlement", "followups"):
        setattr(pipeline, name, SimpleNamespace(run_once=lambda: {"status": "CLEAR"}))

    def build(
        source="s3", producer="s6", *, reason="NO_DUE_TASK", count=0, seconds=0.01
    ):
        target = SimpleNamespace(
            worker=SimpleNamespace(
                source=source,
                scope=SimpleNamespace(producer=producer, consumer=producer),
            ),
            demand=lambda **_: 16 if producer == "s6" else 1,
            progress=dict,
            runs=0,
            reason=reason,
            count=count,
            seconds=seconds,
            behavior=None,
        )

        def run(**kwargs):
            target.runs += 1
            calls.append((producer + ":" + source, kwargs["budget_ms"], clock[0]))
            elapsed = min(target.seconds, kwargs["budget_ms"] / 1000)
            clock[0] += elapsed
            target.last_run_metrics = {
                "processed": target.count,
                "context_ms": round(elapsed * 1000),
                "elapsed_ms": round(elapsed * 1000),
                "discovery_ms": 7,
            }
            target.last_run_diagnostics = {
                "stage": "COMPLETE",
                "stop_reason": target.reason,
                "failures": [],
            }
            result = {
                f"{producer}:{source}:{target.runs}:{i}": "IGNORED"
                for i in range(target.count)
            }
            return target.behavior(result) if target.behavior else result

        target.run_once = run
        return target

    pipeline.schedulers = (
        build(reason="BUDGET_EXHAUSTED", count=4, seconds=10),
        build(producer="s8"),
    )
    return SimpleNamespace(
        pipeline=pipeline,
        build=build,
        clock=clock,
        calls=calls,
        saved=saved,
        prepared=prepared,
        dispatches=dispatches,
        save_hooks=save_hooks,
        settings=settings,
    )


def test_reclaims_unused_peer_budget_without_larger_deadline_and_sums_metrics(harness):
    h, p = harness, harness.pipeline
    result = p.run_once()
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3", "s6:s3"]
    assert h.clock[0] <= 16
    assert all(start + budget / 1000 <= 16.001 for _, budget, start in h.calls)
    assert result["scheduler_reclaim_order"] == ["s6:s3"]
    metrics = result["scheduler_metrics"]["s6:s3"]
    assert metrics["processed"] == 8
    assert metrics["discovery_ms"] == 14
    assert metrics["context_ms"] == result["phase_elapsed_ms"]["s6"]
    assert len([k for k in result["phases"]["s6"] if k.startswith("s6:")]) == 8
    assert result["scheduler_allocation"]["s6:s3"]["passes"] == 2
    assert [s["round"] for s in result["scheduler_passes"]["s6:s3"]] == [
        "FIRST",
        "RECLAIM",
    ]
    assert h.saved[-1]["scheduler_metrics"] == result["scheduler_metrics"]


def test_batch_limit_has_only_one_extra_turn_even_with_time_left(harness):
    h, p = harness, harness.pipeline
    p.schedulers[0].reason, p.schedulers[0].seconds = "BATCH_LIMIT", 0.01
    result = p.run_once()
    assert p.schedulers[0].runs == 2
    assert h.clock[0] < 0.1
    assert result["scheduler_allocation"]["s6:s3"]["processed"] == 8


@pytest.mark.parametrize(
    "reason,count",
    [
        ("NO_DUE_TASK", 4),
        ("BUDGET_EXHAUSTED", 0),
        ("BATCH_LIMIT", 0),
        ("STOP_REQUESTED", 4),
        ("PREPARED_YIELD", 4),
        ("UNKNOWN", 4),
    ],
)
def test_empty_nonexhausted_or_unknown_lane_is_never_repolled(harness, reason, count):
    p = harness.pipeline
    p.schedulers[0].reason, p.schedulers[0].count = reason, count
    p.run_once()
    assert p.schedulers[0].runs == 1


def test_tv_and_quant_first_rounds_precede_reclaim_and_rotation_advances_once(harness):
    h, p = harness, harness.pipeline
    p.schedulers[0].seconds = 0.01
    p.schedulers += tuple(
        h.build(source, producer)
        for source in ("tv_bridge", "quant")
        for producer in ("s6", "s8")
    )
    first = p.run_once()
    assert [c[0] for c in h.calls] == [
        "s6:tv_bridge",
        "s8:tv_bridge",
        "s6:s3",
        "s8:s3",
        "s6:quant",
        "s8:quant",
        "s6:s3",
    ]
    h.calls.clear()
    second = p.run_once()
    assert first["scheduler_order"][:2] == ["s6:tv_bridge", "s8:tv_bridge"]
    assert second["scheduler_order"][:3] == ["s8:tv_bridge", "s6:tv_bridge", "s8:s3"]
    assert h.calls[-1][0] == "s6:s3"


def test_multiple_reclaim_lanes_receive_one_fair_turn_each(harness):
    h, p = harness, harness.pipeline
    for target in p.schedulers:
        target.reason, target.count, target.seconds = "BATCH_LIMIT", 2, 0.01
    result = p.run_once()
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3", "s6:s3", "s8:s3"]
    assert Counter(c[0] for c in h.calls) == {"s6:s3": 2, "s8:s3": 2}
    assert result["scheduler_reclaim_order"] == ["s6:s3", "s8:s3"]


def test_failed_second_lane_prevents_queued_reclaim(harness):
    h, p = harness, harness.pipeline
    p.schedulers[1].behavior = lambda _: {"status": "BLOCKED"}
    result = p.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3"]


def test_diagnostic_failure_disqualifies_reclaim_even_with_terminal_results(harness):
    p = harness.pipeline
    target = p.schedulers[0]

    def fail(result):
        target.last_run_diagnostics["failures"] = [{"error_code": "TimeoutError"}]
        return result

    target.behavior = fail
    p.run_once()
    assert target.runs == 1


@pytest.mark.parametrize(
    "dispatch_result", ["FILLED", "ACKNOWLEDGED", "UNKNOWN", "DENIED"]
)
def test_prepared_dispatch_yields_before_any_reclaim(harness, dispatch_result):
    h, p = harness, harness.pipeline
    p.enable_entries = True

    def prepare(_):
        h.prepared.append("qa-prepared")
        return {"candidate": "PREPARED"}

    p.schedulers[1].behavior = prepare

    def dispatch(order):
        h.dispatches.append(order)
        h.prepared.remove(order)
        return dispatch_result

    p.runtime.execution.dispatch = dispatch
    p._confirmed_capacity_decline = lambda _: None
    result = p.run_once()
    assert h.dispatches == ["qa-prepared"]
    assert len(h.calls) == 2
    assert "scheduler_reclaim_order" not in result
    if dispatch_result in {"UNKNOWN", "DENIED"}:
        assert result["status"] == "ENTRY_BLOCKED"


def test_prepared_in_nondispatch_mode_still_prevents_extra_reclaim(harness):
    p = harness.pipeline
    p.schedulers[1].behavior = lambda _: {"candidate": "PREPARED"}
    result = p.run_once()
    assert len(harness.calls) == 2
    assert result["scheduler_reclaim_skipped"] == "PREPARED_OBSERVED"


def test_drain_after_first_round_stops_queued_reclaim(harness):
    h, p = harness, harness.pipeline
    stop = [False]
    p.schedulers[1].behavior = lambda result: stop.__setitem__(0, True) or result
    result = p.run_once(stop_requested=lambda: stop[0])
    assert result["drain_requested"] is True
    assert len(h.calls) == 2


def test_peer_overrun_cannot_extend_original_budget(harness):
    h, p = harness, harness.pipeline

    def overrun(result):
        h.clock[0] += 20
        return result

    p.schedulers[1].behavior = overrun
    result = p.run_once()
    assert len(h.calls) == 2
    assert result["evaluation_stop_reason"] == "CYCLE_BUDGET_EXHAUSTED"


def test_reclaim_exception_preserves_prior_metrics_and_terminal_results(harness):
    p = harness.pipeline
    target = p.schedulers[0]

    def fail_on_second(result):
        if target.runs == 2:
            raise TimeoutError("private details")
        return result

    target.behavior = fail_on_second
    result = p.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert result["scheduler_metrics"]["s6:s3"]["processed"] == 8
    assert len([k for k in result["phases"]["s6"] if k.startswith("s6:")]) == 4
    assert result["phases"]["s6"]["error_code"] == "TimeoutError"
    assert "private details" not in str(result)


@pytest.mark.parametrize("interrupt", ["DEADLINE", "DRAIN"])
def test_journal_delay_or_drain_cannot_launch_reclaim_after_its_check(
    harness, interrupt
):
    h, p = harness, harness.pipeline
    stop = [False]

    def after_save(journal):
        if journal.get("scheduler_reclaim_order") and journal["phases"].get("s6") == {
            "status": "STARTED"
        }:
            if interrupt == "DEADLINE":
                h.clock[0] = 100
            else:
                stop[0] = True

    h.save_hooks.append(after_save)
    result = p.run_once(stop_requested=lambda: stop[0])
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3"]
    assert result["scheduler_metrics"]["s6:s3"]["processed"] == 4
    assert result["scheduler_passes"]["s6:s3"][-1]["metrics"]["processed"] == 0
    assert result["scheduler_diagnostics"]["s6:s3"]["stop_reason"] == (
        "BUDGET_EXHAUSTED" if interrupt == "DEADLINE" else "STOP_REQUESTED"
    )


def test_preparation_during_reclaim_stops_other_reclaim_turns(harness):
    h, p = harness, harness.pipeline
    p.enable_entries = True
    for target in p.schedulers:
        target.reason, target.count, target.seconds = "BATCH_LIMIT", 2, 0.01
    first = p.schedulers[0]

    def prepare_on_second(result):
        if first.runs == 2:
            h.prepared.append("qa-reclaim-prepared")
            return {"candidate": "PREPARED"}
        return result

    first.behavior = prepare_on_second
    result = p.run_once()
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3", "s6:s3"]
    assert h.dispatches == ["qa-reclaim-prepared"]
    assert result["evaluation_stop_reason"] == "DISPATCH_YIELD"


def expected_wait_diagnostic(code="EXISTING_SYMBOL_POSITION", stage="CONTEXT"):
    identifier = str(UUID(int=1))
    reason = module.candidate_wait_reason(ValueError(code), stage=stage)
    return identifier, {
        "stage": "COMPLETE",
        "stop_reason": "BUDGET_EXHAUSTED",
        "failures": [
            {
                "signal_id": identifier,
                "attempt": 1,
                "stage": stage,
                "error_code": code,
                "outcome": "DEFERRED",
            }
        ],
        "candidate_waits": [
            {
                "signal_id": identifier,
                "symbol": "BTCUSDT",
                "scope": "CANDIDATE",
                "reason": reason,
                "error_code": code,
                "retry_seconds": 5,
                "signal_expires_at_ms": 2000000,
                "execution_authorized": False,
            }
        ],
    }


@pytest.mark.parametrize(
    "code,stage",
    [
        ("EXISTING_SYMBOL_POSITION", "CONTEXT"),
        ("ADMISSION_BUDGET_UNAVAILABLE", "CONTEXT"),
        ("ACCOUNT_RISK_CAPACITY_UNAVAILABLE", "CONSUME"),
        ("ENTRY_RESEARCH_CONTEXT_EXPIRED", "CONTEXT"),
    ],
)
def test_verified_candidate_waits_allow_one_reclaim_without_changing_wait(
    harness, code, stage
):
    h, p = harness, harness.pipeline
    target = p.schedulers[0]
    target.seconds = 0.01
    identifier, diagnostic = expected_wait_diagnostic(code, stage)

    def expected(result):
        # The existing leased scheduler, not the pipeline, chooses the wait
        # and delay. The extra pass processes a distinct ready signal.
        if target.runs == 1:
            target.last_run_diagnostics = deepcopy(diagnostic)
            return {identifier: "DEFERRED"}
        return result

    target.behavior = expected
    result = p.run_once()
    assert [c[0] for c in h.calls] == ["s6:s3", "s8:s3", "s6:s3"]
    assert result["phases"]["s6"][identifier] == "DEFERRED"
    assert result["scheduler_passes"]["s6:s3"][0]["diagnostics"] == diagnostic
    assert h.dispatches == []
    assert result["status"] == "CYCLE_COMPLETE"


@pytest.mark.parametrize(
    "mutation",
    [
        "REAL_FAILURE",
        "FAILURE_UNAVAILABLE",
        "RESULT_UNAVAILABLE",
        "WRONG_ID",
        "WRONG_CODE",
        "UNKNOWN_CODE",
        "WRONG_SCOPE",
        "AUTHORIZED",
        "MISSING_AUTHORIZED",
        "WRONG_REASON",
        "MISSING_WAIT",
        "DUPLICATE_WAIT",
        "ACK_STAGE",
        "MALFORMED_STAGE",
        "BAD_UUID",
        "BAD_ATTEMPT",
        "BAD_RETRY",
        "BAD_EXPIRY",
        "CONTEXT_CODE_AT_CONSUME",
    ],
)
def test_reclaim_wait_requires_exact_existing_whitelist_and_matching_proof(
    harness, mutation
):
    h, p = harness, harness.pipeline
    target = p.schedulers[0]
    target.seconds = 0.01
    identifier, diagnostic = expected_wait_diagnostic()
    result_for_wait = {identifier: "DEFERRED"}
    failure = diagnostic["failures"][0]
    wait = diagnostic["candidate_waits"][0]
    if mutation == "REAL_FAILURE":
        diagnostic["failures"].append(
            {
                "signal_id": str(UUID(int=2)),
                "attempt": 1,
                "stage": "CONTEXT",
                "error_code": "TimeoutError",
                "outcome": "UNAVAILABLE",
            }
        )
    elif mutation == "FAILURE_UNAVAILABLE":
        failure["outcome"] = "UNAVAILABLE"
    elif mutation == "RESULT_UNAVAILABLE":
        result_for_wait[identifier] = "UNAVAILABLE"
    elif mutation == "WRONG_ID":
        wait["signal_id"] = str(UUID(int=2))
    elif mutation == "WRONG_CODE":
        wait["error_code"] = "ADMISSION_BUDGET_UNAVAILABLE"
    elif mutation == "UNKNOWN_CODE":
        wait["error_code"] = failure["error_code"] = "UNKNOWN_CAPACITY_WAIT"
    elif mutation == "WRONG_SCOPE":
        wait["scope"] = "ACCOUNT"
    elif mutation == "AUTHORIZED":
        wait["execution_authorized"] = True
    elif mutation == "MISSING_AUTHORIZED":
        del wait["execution_authorized"]
    elif mutation == "WRONG_REASON":
        wait["reason"] = "SYMBOL_DATA_MISSING"
    elif mutation == "MISSING_WAIT":
        diagnostic.pop("candidate_waits")
    elif mutation == "DUPLICATE_WAIT":
        diagnostic["candidate_waits"].append(dict(wait))
    elif mutation == "ACK_STAGE":
        failure["stage"] = "ACK"
    elif mutation == "MALFORMED_STAGE":
        failure["stage"] = []
    elif mutation == "BAD_UUID":
        wait["signal_id"] = failure["signal_id"] = "not-an-id"
        result_for_wait = {"not-an-id": "DEFERRED"}
    elif mutation == "BAD_ATTEMPT":
        failure["attempt"] = True
    elif mutation == "BAD_RETRY":
        wait["retry_seconds"] = 0
    elif mutation == "BAD_EXPIRY":
        wait["signal_expires_at_ms"] = -1
    elif mutation == "CONTEXT_CODE_AT_CONSUME":
        failure["stage"] = "CONSUME"
        failure["error_code"] = wait["error_code"] = "ENTRY_RESEARCH_CONTEXT_EXPIRED"
        wait["reason"] = "CANDIDATE_CONTEXT_EXPIRED"

    def mutated(_):
        target.last_run_diagnostics = deepcopy(diagnostic)
        return result_for_wait

    target.behavior = mutated
    result = p.run_once()
    assert target.runs == 1
    assert "scheduler_reclaim_order" not in result
    assert h.dispatches == []
