"""Local capacity refusal must not starve peers or weaken submission safety."""

from dataclasses import replace

import pytest
from test_v2_trading_pipeline import case as pipeline_case

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountRiskDenied
from v2_core.runtime_policy import PolicyStore, resolve

database = database_fixture
case = pipeline_case


def inject_atomic_decline(pipeline, monkeypatch, code):
    # Exercise the real runner and real durable cancellation, with only the
    # atomic risk verdict injected. No exchange request is permitted in QA.
    execution = pipeline.runtime.execution
    original = execution.orders.transition

    def transition(order_id, **kwargs):
        if kwargs["status"] == "SUBMITTING":
            raise AccountRiskDenied(code, {"source": "explicit-QA-risk-verdict"})
        return original(order_id, **kwargs)

    monkeypatch.setattr(execution.orders, "transition", transition)
    monkeypatch.setattr(execution, "risk_check", lambda _: True)
    monkeypatch.setattr(execution, "risk_reference", None)
    monkeypatch.setattr(execution, "submit", lambda _: pytest.fail("must not submit"))


@pytest.mark.parametrize(
    "code",
    [
        "CAPITAL_PORTFOLIO_RISK_EXCEEDED",
        "CAPITAL_SINGLE_TRADE_RISK_EXCEEDED",
    ],
)
def test_durable_capacity_decline_continues_peer_without_order_retry(
    case, monkeypatch, code
):
    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, code)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert "s8" in result["phases"]  # previously stranded until the next cycle
    assert result["capacity_declines"]
    assert set(result["capacity_declines"].values()) == {code}
    assert set(result["entries"].values()) == {"DENIED"}
    assert "entry_dispatch" not in result["phases"]
    for order_id in result["entries"]:
        assert pipeline._confirmed_capacity_decline(order_id) == code
        assert pipeline.runtime.execution.snapshot(order_id)["version"] == 2
    again = pipeline.run_once()
    assert again["entries"] == {}  # no resend / replay of completed candidates


def test_same_consumer_drains_after_proven_local_capacity_decline(
    case, database, monkeypatch
):
    from v2_core.signals import Signals

    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    market = pipeline.market.run_once

    def two_candidates():
        result = market()
        with database() as conn:
            snapshot = conn.execute(
                "SELECT snapshot FROM v2_inbound_signals "
                "WHERE source='s3' AND snapshot->>'signal'='TREND_UP' LIMIT 1"
            ).fetchone()[0]
        Signals(database).admit(
            source="s3",
            environment="SANDBOX",
            request_key="second-candidate",
            snapshot=snapshot,
        )
        return result

    monkeypatch.setattr(pipeline.market, "run_once", two_candidates)
    result = pipeline.run_once()
    assert result["status"] == "CYCLE_COMPLETE", result
    assert len(result["capacity_declines"]) == 2
    assert result["scheduler_allocation"]["s6:s3"]["passes"] >= 2
    assert "s8" in result["phases"]
    for order in result["entries"]:
        assert pipeline.runtime.execution.snapshot(order)["version"] == 2


@pytest.mark.parametrize(
    "code", ["REFERENCE_STALE", "POLICY_REQUIRED", "CAPITAL_SNAPSHOT_STALE"]
)
def test_other_risk_errors_remain_fail_closed(case, monkeypatch, code):
    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, code)
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert "s8" not in result["phases"]
    assert not result.get("capacity_declines")


@pytest.mark.parametrize(
    "patch",
    [
        {"scheduler.max_capacity_declines": 1},
        {"scheduler.continue_after_capacity_denial": False},
    ],
)
def test_dynamic_bound_and_kill_switch(case, database, monkeypatch, patch):
    pipeline, _, _, _, _ = case
    PolicyStore(database, pipeline.scope).patch(
        patch, expected_version=0, reason="bounded local decline test"
    )
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    result = pipeline.run_once()
    assert len(result["entries"]) == 1
    assert "s8" not in result["phases"]
    if patch.get("scheduler.max_capacity_declines"):
        assert result["admission_stop_reason"] == "CAPACITY_EVALUATION_BUDGET"
        assert result["status"] == "CYCLE_COMPLETE"
    else:
        assert result["status"] == "ENTRY_BLOCKED"


def test_proof_read_failure_does_not_allow_next_candidate(case, monkeypatch):
    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")

    def unavailable(_):
        raise TimeoutError("private-connection-details")

    monkeypatch.setattr(pipeline, "_confirmed_capacity_decline", unavailable)
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert result["capacity_check_error"] == "TimeoutError"
    assert "s8" not in result["phases"]
    assert "private-connection-details" not in str(result)


def test_capacity_proof_cannot_cross_account_boundary(case, monkeypatch):
    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    result = pipeline.run_once()
    order_id = next(iter(result["entries"]))
    pipeline.scope = replace(pipeline.scope, account_id="other-account")
    assert pipeline._confirmed_capacity_decline(order_id) is None


def test_post_submit_cancel_is_not_a_local_capacity_decline(case, monkeypatch):
    pipeline, _, _, _, _ = case
    pipeline.run_once()  # prepare without dispatch
    with pipeline.connect() as conn:
        order_id = conn.execute(
            "SELECT order_id::text FROM v2_orders LIMIT 1"
        ).fetchone()[0]
    orders = pipeline.runtime.data.orders
    orders.transition(order_id, expected_version=1, status="SUBMITTING", evidence={})
    orders.transition(
        order_id,
        expected_version=2,
        status="CANCELLED",
        evidence={
            "fills_complete": True,
            "reason": "account risk denied",
            "account_risk_code": "CAPITAL_PORTFOLIO_RISK_EXCEEDED",
        },
    )
    assert pipeline._confirmed_capacity_decline(order_id) is None


def test_capacity_continuation_honors_cycle_deadline(case, monkeypatch):
    import services.v2_trading_pipeline as module

    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    original = pipeline.runtime.execution.dispatch

    def dispatch(order_id):
        result = original(order_id)
        clock[0] += 100
        return result

    monkeypatch.setattr(pipeline.runtime.execution, "dispatch", dispatch)
    result = pipeline.run_once()
    assert len(result["entries"]) == 1
    assert result["admission_stop_reason"] == "CAPACITY_EVALUATION_BUDGET"
    assert "s8" not in result["phases"]


def test_existing_prepared_is_dispatched_before_any_new_evaluation(case, monkeypatch):
    pipeline, calls, _, _, _ = case
    pipeline.run_once()  # an existing durable prepared order
    pipeline.enable_entries = True
    calls.clear()
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    original_dispatch = pipeline.runtime.execution.dispatch
    monkeypatch.setattr(
        pipeline.runtime.execution,
        "dispatch",
        lambda oid: calls.append("dispatch") or original_dispatch(oid),
    )
    original_schedule = pipeline.schedulers[0].run_once
    monkeypatch.setattr(
        pipeline.schedulers[0],
        "run_once",
        lambda **kw: calls.append("schedule") or original_schedule(**kw),
    )
    result = pipeline.run_once()
    assert result["capacity_declines"]
    assert calls.index("dispatch") < calls.index("schedule")
    assert calls.index("protection") < calls.index("dispatch")


def test_peer_failure_after_capacity_refusal_remains_blocked(case, monkeypatch):
    pipeline, _, _, _, _ = case
    pipeline.enable_entries = True
    inject_atomic_decline(pipeline, monkeypatch, "CAPITAL_PORTFOLIO_RISK_EXCEEDED")
    monkeypatch.setattr(
        pipeline.schedulers[1], "run_once", lambda **kw: {"status": "UNAVAILABLE"}
    )
    result = pipeline.run_once()
    assert result["capacity_declines"]
    assert result["status"] == "ENTRY_BLOCKED"


@pytest.mark.parametrize("value", [0, 21, True, "4"])
def test_capacity_attempt_budget_validated(value):
    with pytest.raises(ValueError):
        resolve({"scheduler.max_capacity_declines": value})


@pytest.mark.parametrize("blocked", [False, True])
def test_capacity_diagnostic_does_not_hide_lag_or_failures(database, blocked):
    import json
    import time
    from dataclasses import asdict

    from services.v2_trading_health import TradingHealth
    from tests.support.v2_admission import scheduler_signal
    from v2_core.state import BusinessState, StateKey

    now = time.time_ns() // 1000000 + 31000
    scheduler_signal(database, "lag-with-capacity")
    health = TradingHealth(
        database, account_id="test-account", tv_enabled=True, clock_ms=lambda: now
    )
    store = BusinessState(database)
    store.change(
        StateKey(
            **asdict(health.scope), namespace="trading-pipeline-cycle-v1", key="qa"
        ),
        expected_version=0,
        request_key="qa",
        reason="QA",
        payload={
            "cycle_id": "qa",
            "started_at_ms": now - 1000,
            "finished_at_ms": now,
            "status": "ENTRY_BLOCKED" if blocked else "CYCLE_COMPLETE",
            "phases": {"s8": {"status": "UNAVAILABLE"}} if blocked else {},
            "capacity_declines": {"qa-order": "CAPITAL_PORTFOLIO_RISK_EXCEEDED"},
        },
    )
    health()
    payload = json.loads(store.read(health.key).payload_json)
    assert "SIGNAL_CONSUMPTION_LAG" in payload["findings"]
    assert ("PIPELINE_ENTRY_BLOCKED" in payload["findings"]) == blocked
    assert payload["expected_waits"]["opening_capacity"] == {
        "reason": "LOCAL_PRE_SUBMIT_CAPACITY_DECLINE",
        "count": 1,
        "codes": ["CAPITAL_PORTFOLIO_RISK_EXCEEDED"],
        "cycle_id": "qa",
        "observed_at_ms": now,
    }
