"""Immutable full-plan risk budgets; no venue or running service access."""

from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.service import TradingData
from v2_core.strategy_capital import (
    CAPITAL_EVIDENCE_SCHEMA,
    CAPITAL_SCHEMA,
    STRATEGY_CAPITAL_RECORD_SQL,
    StrategyCapitalPlan,
    read_strategy_capital,
    read_strategy_capital_for_episode,
)
from v2_core.strategy_plan import TradePlan
from v2_core.strategy_plan_store import read_trade_plan, read_trade_plan_record

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-strategy-capital", "SANDBOX", "FUTURES")


def material(*, config=None, plan_changes=None, features=None):
    config = (
        config
        if config is not None
        else {
            "capital": {
                "schema": CAPITAL_SCHEMA,
                "roundtrip_cost_fraction": "0.002",
                "price_drift_fraction": "0.01",
            }
        }
    )
    config_digest = digest(canonical(config))
    plan = TradePlan(
        **{
            "scope": SCOPE,
            "instance_id": "trend-a",
            "strategy_id": "trend",
            "code_version": "v1",
            "deployment_generation": 1,
            "config_digest": config_digest,
            "opportunity_id": "opportunity-a",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "quantity": "2.5",
            "entry_price": "100",
            "stop_price": "95",
            "planned_loss": "13",
            "quantity_step": "0.25",
            "leverage": 3,
            "margin_mode": "ISOLATED",
            "observed_at_ms": 1,
            "decided_at_ms": 2,
            "expires_at_ms": 1000,
            "exit_handler": "exit-v1",
            "exit_version": "v1",
            "exit_config_json": "{}",
            **(plan_changes or {}),
        }
    )
    record = {
        "scope": asdict(SCOPE),
        "producer": plan.instance_id,
        "strategy_version": plan.code_version,
        "config_digest": config_digest,
        "payload": {
            "symbol": plan.symbol,
            "side": plan.side,
            "quantity": plan.quantity,
            "strategy_version": plan.code_version,
            "config_digest": config_digest,
        },
        "evidence_strategy_version": plan.code_version,
        "evidence_config_digest": config_digest,
        "config": config,
        "snapshot": {
            "strategy_plan": plan.payload(),
            "symbol": plan.symbol,
            "observed_at": plan.observed_at_ms,
            "decided_at": plan.decided_at_ms,
            "expires_at_ms": plan.expires_at_ms,
            "features": features if features is not None else {},
        },
    }
    return plan, record


def projection(**kwargs):
    return read_strategy_capital(material(**kwargs)[1], scope=SCOPE)


def test_legacy_null_is_only_nonerror_absence():
    assert read_strategy_capital(None, scope=SCOPE) is None
    for value in ({}, [], 0, {"snapshot": {}}, {"snapshot": None}):
        with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RECORD_INVALID"):
            read_strategy_capital(value, scope=SCOPE)
    _, record = material()
    record["snapshot"].pop("strategy_plan")
    assert read_trade_plan_record(record) is None
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RECORD_INVALID"):
        read_strategy_capital(record, scope=SCOPE)


def test_valid_record_projects_original_plan_and_no_optional_leverage():
    original, record = material()
    result = read_strategy_capital(record, scope=SCOPE)
    assert result.plan == original
    assert result.plan is not original
    assert result.cost_fraction == Decimal("0.002")
    assert result.price_drift_fraction == Decimal("0.01")
    assert result.stop_fraction == Decimal("0.05")
    assert result.leverage_proof is None
    # Entries are long expired; historical risk remains measurable.
    assert result.plan.entry_valid_at(1001) is False
    assert result.stop_risk("250", current_cost_fraction="0.001") == Decimal("15.505")


@pytest.mark.parametrize(
    "path,value",
    [
        (("scope", "account_id"), "different-account"),
        (("scope", "environment"), "LIVE"),
        (("producer",), "other-instance"),
        (("strategy_version",), "v2"),
        (("evidence_strategy_version",), "v2"),
        (("config_digest",), "a" * 64),
        (("evidence_config_digest",), "a" * 64),
        (("payload", "config_digest"), "a" * 64),
        (("payload", "strategy_version"), "v2"),
        (("payload", "symbol"), "ETHUSDT"),
        (("payload", "side"), "SELL"),
        (("payload", "quantity"), "2.75"),
        (("snapshot", "symbol"), "ETHUSDT"),
        (("snapshot", "observed_at"), True),
        (("snapshot", "decided_at"), 3),
        (("snapshot", "expires_at_ms"), 999),
        (("config", "different"), "configuration"),
        (("snapshot", "strategy_plan"), None),
        (("snapshot", "strategy_plan"), {}),
    ],
)
def test_every_frozen_identity_mismatch_fails_closed(path, value):
    _, record = material()
    cursor = record
    for name in path[:-1]:
        cursor = cursor[name]
    cursor[path[-1]] = value
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
        read_strategy_capital(record, scope=SCOPE)


def test_scope_must_be_explicit_and_exact():
    _, record = material()
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_SCOPE_REQUIRED"):
        read_strategy_capital(record, scope=asdict(SCOPE))
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_SCOPE_MISMATCH"):
        read_strategy_capital(record, scope=replace(SCOPE, environment="LIVE"))
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_SCOPE_MISMATCH"):
        read_strategy_capital(record, scope=replace(SCOPE, account_id="other-account"))


def test_missing_capital_config_is_not_a_legacy_fallback():
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_CONFIG_REQUIRED"):
        projection(config={})


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"schema": "old"},
        {
            "schema": CAPITAL_SCHEMA,
            "roundtrip_cost_fraction": "0.002",
            "price_drift_fraction": "0.01",
            "extra": True,
        },
    ],
)
def test_capital_config_shape_errors_are_fixed_code(value):
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_CONFIG_INVALID"):
        projection(config={"capital": value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("roundtrip_cost_fraction", "0"),
        ("roundtrip_cost_fraction", "0.000099999999999999"),
        ("roundtrip_cost_fraction", "0.100000000000000001"),
        ("roundtrip_cost_fraction", 1),
        ("roundtrip_cost_fraction", None),
        ("roundtrip_cost_fraction", "NaN"),
        ("roundtrip_cost_fraction", "Infinity"),
        ("price_drift_fraction", "0.000099999999999999"),
        ("price_drift_fraction", "0.050000000000000001"),
        ("price_drift_fraction", "-0.01"),
        ("price_drift_fraction", True),
    ],
)
def test_cost_and_drift_bounds(field, value):
    _, record = material()
    config = record["config"]
    config["capital"][field] = value
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_CONFIG_INVALID"):
        projection(config=config)


@pytest.mark.parametrize("cost,drift", [("0.0001", "0.0001"), ("0.1", "0.05")])
def test_cost_and_drift_bounds_are_inclusive(cost, drift):
    result = projection(
        config={
            "capital": {
                "schema": CAPITAL_SCHEMA,
                "roundtrip_cost_fraction": cost,
                "price_drift_fraction": drift,
            }
        }
    )
    assert result.cost_fraction == Decimal(cost)
    assert result.price_drift_fraction == Decimal(drift)


def test_frozen_cost_cannot_be_lowered_but_current_cost_can_raise_budget():
    result = projection()
    assert result.stop_risk("250", current_cost_fraction="0") == Decimal("15.505")
    assert result.stop_risk("250", current_cost_fraction="0.002") == Decimal("15.505")
    assert result.stop_risk("250", current_cost_fraction="0.01") == Decimal("17.525")
    # Current adverse price is independently budgeted against the original stop.
    assert result.stop_risk("275", current_cost_fraction="0.002") == Decimal("38.05")


def test_short_direction_and_crossed_stop_retain_full_frozen_risk():
    result = projection(plan_changes={"side": "SELL", "stop_price": "105"})
    assert result.stop_risk("250", current_cost_fraction="0.001") == Decimal("15.495")
    assert result.stop_risk("225", current_cost_fraction="0.002") == Decimal("37.95")
    # Current price above a short stop must not create negative risk/free budget.
    assert result.stop_risk("275", current_cost_fraction="0.002") == Decimal("15.495")
    long = projection()
    assert long.stop_risk("225", current_cost_fraction="0.002") == Decimal("15.505")
    assert long.stop_risk("0", current_cost_fraction="0.002") == Decimal("15.505")


def test_explicit_planned_loss_remains_floor_after_all_prices_move():
    result = projection(plan_changes={"planned_loss": "20"})
    assert result.stop_risk("0", current_cost_fraction="0.002") == Decimal(20)
    assert result.stop_risk("250", current_cost_fraction="0.002") == Decimal(20)


def test_non_power_of_ten_step_and_tiny_units_are_context_independent():
    result = projection(
        plan_changes={
            "quantity": "0.075",
            "quantity_step": "0.025",
            "entry_price": "0.0000000000000003",
            "stop_price": "0.0000000000000001",
            "planned_loss": "0.000000000000000015",
        }
    )
    normal = result.stop_risk("0.000000000000000023", current_cost_fraction="0.002")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert (
            result.stop_risk("0.000000000000000023", current_cost_fraction="0.002")
            == normal
        )
        assert result.stop_fraction == Decimal("0.666666666666666667")
    assert normal == Decimal("0.000000000000000016")
    exact_fraction = Fraction(2, 3)
    assert Fraction(result.stop_fraction) >= exact_fraction
    assert Fraction(result.stop_fraction) - exact_fraction < Fraction(1, 10**18)


@pytest.mark.parametrize(
    "notional,cost",
    [
        ("-1", "0.002"),
        ("250", "-0.001"),
        (250.0, "0.002"),
        ("NaN", "0.002"),
        ("250", True),
        ("250", "Infinity"),
    ],
)
def test_bad_current_risk_input_has_fixed_error(notional, cost):
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RISK_INPUT_INVALID"):
        projection().stop_risk(notional, current_cost_fraction=cost)


def test_risk_ceiling_overflow_fails_closed():
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RISK_OUT_OF_RANGE"):
        projection().stop_risk("99999999999999999999", current_cost_fraction="99999")


def test_nested_leverage_proof_is_defensively_copied():
    from v2_core.adaptive_leverage import BOOST_CHECKS

    proof = {
        "selected": 3,
        "venue": {
            "symbol": "BTCUSDT",
            "observed_at_ms": 1,
            "book_at_ms": 1,
            "spread_fraction": "0.0001",
            "top_notional": "100000",
            "tiers": [
                {"floor": "0", "cap": "1000", "max_leverage": 20, "mmr": "0.005"}
            ],
        },
        "checks": {name: True for name in BOOST_CHECKS},
    }
    _, record = material(
        features={
            "capital": {
                "schema": CAPITAL_EVIDENCE_SCHEMA,
                "leverage_decision": proof,
            }
        }
    )
    result = read_strategy_capital(record, scope=SCOPE)
    original = deepcopy(result.leverage_proof)
    proof["venue"]["tiers"][0]["cap"] = "1"
    result.leverage_proof["venue"]["tiers"][0]["cap"] = "2"
    record["snapshot"]["strategy_plan"]["stop_price"] = "94"
    assert result.leverage_proof == original
    assert result.plan.stop_price == "95"


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        [],
        {},
        {"schema": "old", "leverage_decision": {}},
        {"schema": CAPITAL_EVIDENCE_SCHEMA, "leverage_decision": None},
        {"schema": CAPITAL_EVIDENCE_SCHEMA, "leverage_decision": {"selected": True}},
        {"schema": CAPITAL_EVIDENCE_SCHEMA, "leverage_decision": {"selected": 2}},
        {
            "schema": CAPITAL_EVIDENCE_SCHEMA,
            "leverage_decision": {"selected": 3, "venue": []},
        },
        {
            "schema": CAPITAL_EVIDENCE_SCHEMA,
            "leverage_decision": {"selected": 3, "checks": None},
        },
        {
            "schema": CAPITAL_EVIDENCE_SCHEMA,
            "leverage_decision": {"selected": 3, "confidence": 0.5},
        },
        {
            "schema": CAPITAL_EVIDENCE_SCHEMA,
            "leverage_decision": {"selected": 3, "secret": "must-not-be-recorded"},
        },
    ],
)
def test_present_bad_optional_proof_cannot_fall_back(evidence):
    _, record = material()
    record["snapshot"]["features"]["capital"] = evidence
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_EVIDENCE_INVALID"):
        read_strategy_capital(record, scope=SCOPE)


@pytest.mark.parametrize("features", [None, [], "corrupt"])
def test_bad_features_container_is_not_missing_proof(features):
    _, record = material()
    record["snapshot"]["features"] = features
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_EVIDENCE_INVALID"):
        read_strategy_capital(record, scope=SCOPE)


def test_reader_issues_one_query_and_keeps_marker_null_distinct():
    _, record = material()
    calls = []
    conn = SimpleNamespace(
        execute=lambda *args: (
            calls.append(args) or SimpleNamespace(fetchone=lambda: (record,))
        )
    )
    episode = uuid4()
    assert (
        read_strategy_capital_for_episode(conn, episode, scope=SCOPE).plan
        == projection().plan
    )
    assert len(calls) == 1
    assert calls[0][1] == (str(episode), *asdict(SCOPE).values())
    assert STRATEGY_CAPITAL_RECORD_SQL in calls[0][0]
    assert "'features'->'evaluation'" not in STRATEGY_CAPITAL_RECORD_SQL
    record["snapshot"]["strategy_plan"] = None
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
        read_strategy_capital_for_episode(conn, episode, scope=SCOPE)


def test_missing_episode_is_not_a_legacy_row():
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None))
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_REQUIRED"):
        read_strategy_capital_for_episode(conn, uuid4(), scope=SCOPE)


def test_constructor_refuses_float_policy_values():
    plan, _ = material()
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_CONFIG_INVALID"):
        StrategyCapitalPlan(plan, 0.002, Decimal("0.01"))


def test_sql_batch_projection_agrees_with_episode_plan_and_prunes_legacy_history(
    database,
):
    plan, record = material(features={"evaluation": {"history": ["huge"] * 100}})
    snapshot = {**record["snapshot"], "source": "qa", "rationale": "capital projection"}
    evidence = DecisionEvidence(
        plan.code_version, canonical(record["config"]), canonical(snapshot)
    )
    intent = OpenIntent(
        str(uuid4()),
        *asdict(SCOPE).values(),
        plan.instance_id,
        str(uuid4()),
        plan.symbol,
        plan.side,
        plan.quantity,
        plan.code_version,
        plan.config_digest,
        evidence.evidence_ref,
    )
    assert TradingData(database).accept(intent, evidence).code == AdmissionCode.ACCEPTED
    with database() as conn:
        assert read_trade_plan(conn, intent.intent_id) == plan
        row = conn.execute(
            "SELECT "
            + STRATEGY_CAPITAL_RECORD_SQL
            + " FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)"
            + " WHERE i.intent_id=%s",
            (intent.intent_id,),
        ).fetchone()
        assert row[0]["snapshot"]["features"] == {}
        assert read_strategy_capital(row[0], scope=SCOPE).plan == plan
        assert (
            read_strategy_capital_for_episode(conn, intent.intent_id, scope=SCOPE).plan
            == plan
        )
        with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_REQUIRED"):
            read_strategy_capital_for_episode(
                conn, intent.intent_id, scope=replace(SCOPE, account_id="other-account")
            )
