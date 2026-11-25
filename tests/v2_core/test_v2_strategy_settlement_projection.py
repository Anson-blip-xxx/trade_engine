"""Typed settlement diagnostics bind original plans; never authorize an entry."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_deployment_execution import SCOPE, stored

from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_settlement_entry import settlement_result_valid
from services.v2_trading_health import settlement_health_projection
from services.v2_trading_pipeline import _stage_clear
from tests.support.v2_admission import database as database_fixture

database = database_fixture


def result_for(episode, plan):
    return {
        "status": "CLEAR",
        "settlements": {
            episode: {
                "status": "SETTLED",
                "episode": episode,
                "net_pnl": "-1.00000001",
                "final_inventory": str(uuid4()),
                "settlement_authorized": False,
            }
        },
        "outcomes": {
            episode: {
                "episode_id": episode,
                "closed_at_ms": plan.decided_at_ms + 1,
                "return_pct": "-1.3",
                "closing_price": "99",
                "plan_digest": plan.plan_digest,
                "outcome_schema": "strategy-outcome-v1",
            }
        },
        "settlement_authorized": False,
    }


def bound_stage(database, scope=SCOPE):
    stage = object.__new__(DirectionalSettlementStage)
    stage.connect, stage.scope = database, scope
    return stage


def test_typed_projection_requires_database_bound_plan_in_pipeline_and_health(database):
    _, plan, _, intent = stored(database)
    result = result_for(intent.intent_id, plan)
    original = deepcopy(result)

    assert not settlement_result_valid(result, status="CLEAR")
    assert settlement_result_valid(
        result, status="CLEAR", connect=database, scope=SCOPE
    )
    assert _stage_clear("settlement", bound_stage(database), result)
    assert settlement_health_projection({"phases": {"settlement": result}}) == (
        False,
        None,
    )
    assert settlement_health_projection(
        {"phases": {"settlement": result}}, connect=database, scope=SCOPE
    ) == (True, None)
    assert result == original
    assert "quality_score" not in result["outcomes"][intent.intent_id]


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_digest",
        "other_real_plan_digest",
        "missing_plan",
        "wrong_schema",
        "quality_score",
        "missing_digest",
        "wrong_episode",
        "wrong_scope",
        "boolean_time",
        "float_return",
        "nonfinite_return",
        "zero_close",
        "unavailable_database",
        "legacy_shape",
    ],
)
def test_unknown_or_forged_typed_result_never_makes_settlement_clear(database, defect):
    _, plan, _, intent = stored(database)
    result = result_for(intent.intent_id, plan)
    record = result["outcomes"][intent.intent_id]
    scope, connect = SCOPE, database
    if defect == "wrong_digest":
        record["plan_digest"] = "a" * 64
    elif defect == "other_real_plan_digest":
        _, other, _, _ = stored(database)
        record["plan_digest"] = other.plan_digest
    elif defect == "missing_plan":
        episode = str(uuid4())
        result["outcomes"] = {episode: {**record, "episode_id": episode}}
    elif defect == "wrong_schema":
        record["outcome_schema"] = "strategy-outcome-v2"
    elif defect == "quality_score":
        record["quality_score"] = "99"
    elif defect == "missing_digest":
        del record["plan_digest"]
    elif defect == "wrong_episode":
        record["episode_id"] = str(uuid4())
    elif defect == "wrong_scope":
        scope = replace(SCOPE, account_id="qa-other-account")
    elif defect == "boolean_time":
        record["closed_at_ms"] = True
    elif defect == "float_return":
        record["return_pct"] = -1.3
    elif defect == "nonfinite_return":
        record["return_pct"] = "NaN"
    elif defect == "zero_close":
        record["closing_price"] = "0"
    elif defect == "unavailable_database":

        def connect():
            raise ConnectionError("private connection details")
    else:
        del record["plan_digest"], record["outcome_schema"]
        record["quality_score"] = "99"

    assert not settlement_result_valid(
        result, status="CLEAR", connect=connect, scope=scope
    )
    assert not _stage_clear("settlement", bound_stage(connect, scope), result)
    assert settlement_health_projection(
        {"phases": {"settlement": result}}, connect=connect, scope=scope
    ) == (False, None)


@pytest.mark.parametrize("marker", [None, {}, "unknown"])
def test_present_malformed_plan_cannot_be_reinterpreted_as_legacy_outcome(
    database, marker
):
    _, plan, _, intent = stored(database, snapshot_changes={"strategy_plan": marker})
    result = result_for(intent.intent_id, plan)
    assert not settlement_result_valid(
        result, status="CLEAR", connect=database, scope=SCOPE
    )
    record = result["outcomes"][intent.intent_id]
    del record["plan_digest"], record["outcome_schema"]
    record["quality_score"] = "99"
    assert not settlement_result_valid(
        result, status="CLEAR", connect=database, scope=SCOPE
    )


def test_expired_plan_still_binds_historical_outcome(database):
    # Historical management does not ask the current deployment or entry clock.
    _, plan, _, intent = stored(
        database,
        plan_changes={
            "observed_at_ms": 1,
            "decided_at_ms": 2,
            "expires_at_ms": 3,
        },
    )
    result = result_for(intent.intent_id, plan)
    assert _stage_clear("settlement", bound_stage(database), result)


def test_derived_failure_remains_diagnostic_and_does_not_read_or_rewrite_plan():
    def unavailable():
        raise AssertionError("derived failure must not trigger new plan or venue I/O")

    episode = str(uuid4())
    result = {
        "status": "CLEAR",
        "settlements": {},
        "outcomes": {
            episode: {"status": "BLOCKED", "error_code": "STRATEGY_OUTCOME_CONFLICT"}
        },
        "settlement_authorized": False,
    }
    assert _stage_clear("settlement", bound_stage(unavailable), result)
    clear, finding = settlement_health_projection(
        {"phases": {"settlement": result}}, connect=unavailable, scope=SCOPE
    )
    assert clear and finding["financial_status"] == "CLEAR"
    assert finding["execution_authorized"] is False


def test_income_lag_scan_includes_typed_closed_episode_and_retains_risk(database):
    from test_v2_strategy_protection import SCOPE as lifecycle_scope
    from test_v2_strategy_protection import new_opening, now

    from services.v2_settlement_entry import entry_clearance, policy_key
    from v2_core.state import BusinessState

    _, _, episode, _, data = new_opening(database)
    closing, _ = data.orders.prepare(
        episode, leg="CLOSE", quantity="0.01", request_key="qa-flat"
    )
    data.orders.transition(
        closing, expected_version=1, status="SUBMITTING", evidence={}
    )
    tick = now()
    data.ledger.record_fill(
        order_id=closing,
        exchange_fill_id="flat",
        quantity="0.01",
        price="105",
        fee="0.001",
        fee_currency="USDT",
        occurred_at_ms=tick,
        evidence={"source": "QA"},
    )
    data.orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        exchange_order_id="11",
        evidence={"fills_complete": True},
    )
    BusinessState(database).change(
        policy_key(lifecycle_scope),
        expected_version=0,
        request_key="qa-lag",
        payload={"enabled": True},
        reason="QA",
    )
    proof = {"observation_id": str(uuid4())}
    stage = bound_stage(database, lifecycle_scope)
    stage.limit, stage.clock = 10, lambda: tick + 1
    stage.coverage = SimpleNamespace(reuse_after_protection=lambda _: proof)
    result = {
        "status": "BLOCKED",
        "settlements": {
            episode: {
                "status": "BLOCKED",
                "reason": "WAITING_INCOME_LAG",
                "settlement_authorized": False,
            }
        },
        "outcomes": {},
        "settlement_authorized": False,
    }
    before = data.trace(episode)
    answer = entry_clearance(stage, result, proof)
    assert answer is not None
    assert answer["status"] == "ENTRY_CLEAR_PENDING_INCOME"
    assert answer["evidence"]["episodes"][0]["episode"] == episode
    assert answer["execution_authorized"] is False
    assert answer["settlement_authorized"] is False
    assert data.trace(episode) == before
    with database() as conn:
        assert conn.execute(
            "SELECT status,released_at FROM v2_risk_reservations WHERE episode_id=%s",
            (episode,),
        ).fetchone() == ("HELD", None)
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone() == (0,)
