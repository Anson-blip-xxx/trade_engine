"""Plan values cannot authorize orders or disable historical stop recovery."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import localcontext

import pytest

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.strategy_plan import (
    LEGACY_DIRECTIONAL_VERSION,
    PLAN_SCHEMA,
    LegacyDirectionalPlan,
    TradePlan,
)
from v2_core.strategy_registry import StrategyRegistry, StrategySpec

SCOPE = AccountScope("BINANCE", "qa-plan-account", "SANDBOX", "FUTURES")
EPISODE = "c207eae0-0507-452a-9aa3-74c610a7912e"


def plan(**changes):
    values = {
        "scope": SCOPE,
        "instance_id": "trend-a",
        "strategy_id": "structural-breakout",
        "code_version": "candidate-v1",
        "deployment_generation": 1,
        "config_digest": "a" * 64,
        "opportunity_id": "binance:sandbox:BTCUSDT:15:structure-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "1.25",
        "entry_price": "100",
        "stop_price": "92",
        "planned_loss": "10.5",
        "quantity_step": ".0025",
        "leverage": 2,
        "margin_mode": "ISOLATED",
        "observed_at_ms": 1000,
        "decided_at_ms": 1001,
        "expires_at_ms": 2000,
        "exit_handler": "structural-exit",
        "exit_version": "exit-v1",
        "exit_config_json": '{"trailing":{"atr_multiple":"3"}}',
        **changes,
    }
    return TradePlan(**values)


def legacy(strategy="S6"):
    side, direction, stop = (
        ("BUY", "LONG", "92") if strategy == "S6" else ("SELL", "SHORT", "108")
    )
    return {
        "scope": SCOPE,
        "producer": strategy.lower(),
        "strategy_version": LEGACY_DIRECTIONAL_VERSION,
        "config": {
            "mode": "TESTNET_ADMISSION_ONLY",
            "account_scope": asdict(SCOPE),
            "strategy": strategy,
        },
        "snapshot": {
            "symbol": "BTCUSDT",
            "features": {
                "evaluation": {
                    "admission_authorized": True,
                    "execution_market_gate": "MARKET_GATES_PASSED",
                    "market_plan": {"strategy": strategy, "side": direction},
                    "sizing": {
                        "reason": "SIZED",
                        "quantity": "1.25",
                        "stop_price": stop,
                    },
                }
            },
            "result": {"action": "OPEN", "side": side, "quantity": "1.25"},
        },
    }


def order(strategy="S6"):
    return {
        "episode_id": EPISODE,
        "symbol": "BTCUSDT",
        "side": "BUY" if strategy == "S6" else "SELL",
        "intent": {"quantity": "1.2500"},
    }


def test_new_plan_round_trip_is_immutable_and_not_an_execution_permit():
    original = plan()
    decoded = TradePlan.from_payload(json.loads(canonical(original.payload())))
    assert decoded == original
    assert decoded.plan_digest == original.plan_digest
    assert original.execution_authorized is False
    with pytest.raises(FrozenInstanceError):
        original.leverage = 8
    returned = original.payload()
    returned["scope"]["account_id"] = "another-account"
    returned["stop_price"] = "1"
    settings = original.exit_config
    settings["trailing"]["atr_multiple"] = "99"
    assert original.scope == SCOPE and original.stop_price == "92"
    assert original.exit_config == {"trailing": {"atr_multiple": "3"}}


def test_plan_owns_a_revalidated_account_snapshot():
    supplied = replace(SCOPE)
    original = plan(scope=supplied)
    original_digest = original.plan_digest
    object.__setattr__(supplied, "account_id", "mutated-caller-object")
    assert original.scope == SCOPE
    assert original.plan_digest == original_digest
    object.__setattr__(supplied, "environment", "INVALID")
    with pytest.raises(ValueError):
        plan(scope=supplied)


@pytest.mark.parametrize("generation", [1, 2**53 - 1])
def test_registered_binding_identity_and_generation_fit_plan_contract(generation):
    spec = StrategySpec(
        "structural-breakout",
        "candidate-v1",
        PLAN_SCHEMA,
        "entry-v1",
        "structural-exit",
    )
    registry = StrategyRegistry(
        [spec],
        entry_handlers=["entry-v1"],
        exit_handlers=["structural-exit"],
        plan_schemas=[PLAN_SCHEMA],
    )
    binding = registry.bind(
        scope=SCOPE,
        instance_id="trend-a",
        strategy_id=spec.strategy_id,
        code_version=spec.code_version,
        config={"risk_fraction": "0.01"},
        generation=generation,
        mode="SHADOW",
    )
    original = plan(
        scope=binding.scope,
        instance_id=binding.instance_id,
        strategy_id=binding.spec.strategy_id,
        code_version=binding.spec.code_version,
        deployment_generation=binding.generation,
        config_digest=binding.config_digest,
        exit_handler=binding.spec.exit_handler,
        schema_version=binding.spec.plan_schema,
    )
    assert original.deployment_generation == generation
    assert original.config_digest == binding.config_digest
    assert not original.execution_authorized and not binding.execution_authorized


@pytest.mark.parametrize(
    "field",
    ["instance_id", "strategy_id", "code_version", "exit_handler", "exit_version"],
)
@pytest.mark.parametrize("value", ["package/module", "package:handler", "_leading"])
def test_handler_and_version_names_use_registry_rules(field, value):
    with pytest.raises(ValueError):
        plan(**{field: value})


def test_new_plan_deadline_only_disables_entry_not_access_to_exit_configuration():
    original = plan()
    assert not original.entry_valid_at(1000)
    assert original.entry_valid_at(1001)
    assert original.entry_valid_at(1999)
    assert not original.entry_valid_at(2000)
    assert not original.entry_valid_at(9_999_999)
    assert original.exit_side == "SELL"
    assert original.exit_version == "exit-v1"
    assert original.stop_price == "92"
    assert original.exit_config["trailing"]["atr_multiple"] == "3"
    with pytest.raises(ValueError):
        original.entry_valid_at(True)


def test_model_research_and_account_changes_get_distinct_plan_digests():
    original = plan()
    variants = [
        replace(original, code_version="candidate-v2"),
        replace(original, deployment_generation=2),
        replace(original, exit_version="exit-v2"),
        replace(original, scope=replace(SCOPE, account_id="qa-another")),
        replace(original, scope=replace(SCOPE, environment="LIVE")),
    ]
    assert len({original.plan_digest, *(row.plan_digest for row in variants)}) == 6
    assert all(row.execution_authorized is False for row in variants)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "strategy-trade-plan-v999"},
        {"scope": asdict(SCOPE)},
        {"instance_id": ""},
        {"strategy_id": "arbitrary\nmodule"},
        {"code_version": " current "},
        {"deployment_generation": True},
        {"deployment_generation": 0},
        {"deployment_generation": 2**53},
        {"config_digest": "short"},
        {"config_digest": "G" * 64},
        {"opportunity_id": ""},
        {"symbol": "BTC/USDT"},
        {"side": "LONG"},
        {"quantity": 1.25},
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "NaN"},
        {"quantity": "1e-19"},
        {"quantity": "1.251"},
        {"quantity_step": "0"},
        {"entry_price": "Infinity"},
        {"stop_price": "100"},
        {"stop_price": "101"},
        {"planned_loss": "9.99"},
        {"planned_loss": "0"},
        {"planned_loss": True},
        {"leverage": True},
        {"leverage": "2"},
        {"leverage": 0},
        {"leverage": 126},
        {"margin_mode": "AUTO"},
        {"observed_at_ms": True},
        {"decided_at_ms": 999},
        {"decided_at_ms": 2000},
        {"expires_at_ms": 1001},
        {"expires_at_ms": 86_401_001},
        {"exit_handler": ""},
        {"exit_version": ""},
        {"exit_config_json": "[]"},
        {"exit_config_json": '{"x":1.25}'},
        {"exit_config_json": '{"x":{"secret":"redacted"}}'},
        {"exit_config_json": '{"x":1,"x":2}'},
        {"exit_config_json": '{"x":{"a":1,"a":2}}'},
        {"exit_config_json": '{"x":"' + "x" * 65536 + '"}'},
        {"exit_config_json": '{"x":"' + "中" * 12000 + '"}'},
        {"exit_config_json": '{"x":"\ud800"}'},
    ],
)
def test_invalid_new_plans_are_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        plan(**changes)


@pytest.mark.parametrize(
    "change", ["unknown", "missing", "scope", "schema", "credential"]
)
def test_payload_decoder_has_exact_fields_and_no_upgrade_fallback(change):
    payload = plan().payload()
    if change == "unknown":
        payload["execution_authorized"] = True
    elif change == "missing":
        del payload["exit_version"]
    elif change == "scope":
        payload["scope"]["environment"] = "ANY"
    elif change == "schema":
        payload["schema_version"] = LEGACY_DIRECTIONAL_VERSION
    else:
        payload["scope"]["api_key"] = "redacted"
    with pytest.raises(ValueError):
        TradePlan.from_payload(payload)


def test_plan_decimal_arithmetic_does_not_depend_on_ambient_precision():
    values = {
        "quantity": "1234.5675",
        "entry_price": "0.123456789123456789",
        "stop_price": "0.113456789123456789",
        "planned_loss": "12.345675",
    }
    normal = plan(**values)
    with localcontext() as ctx:
        ctx.prec = 4
        low_precision = plan(**values)
    assert normal == low_precision
    assert low_precision.plan_digest == normal.plan_digest


def test_long_short_and_unicode_symbols_are_explicit_not_inferred_from_strategy_name():
    long_plan = plan(symbol="牛来USDT", strategy_id="s8")
    short_plan = plan(side="SELL", stop_price="108", strategy_id="s6")
    assert long_plan.exit_side == "SELL"
    assert short_plan.exit_side == "BUY"
    with pytest.raises(ValueError, match="INITIAL_RISK"):
        plan(side="SELL", stop_price="92")


@pytest.mark.parametrize(
    "strategy,exit_side,stop", [("S6", "SELL", "92"), ("S8", "BUY", "108")]
)
def test_legacy_minimal_protection_evidence_needs_no_new_contract_fields(
    strategy, exit_side, stop
):
    evidence = legacy(strategy)
    before = deepcopy(evidence)
    original = LegacyDirectionalPlan.read(**evidence)
    assert evidence == before
    spec = original.protection_spec(order(strategy))
    assert (spec.episode, spec.symbol, spec.side, spec.trigger_price) == (
        EPISODE,
        "BTCUSDT",
        exit_side,
        stop,
    )
    assert original.exit_policy_binding == "LEGACY_CURRENT_ACCOUNT_POLICY"
    assert original.execution_authorized is False
    assert "source" not in evidence["snapshot"]
    assert "context" not in evidence["snapshot"]["features"]
    assert "expires_at_ms" not in evidence["snapshot"]


def test_legacy_projection_does_not_retain_history_or_follow_source_mutations():
    evidence = legacy()
    snapshot = evidence["snapshot"]
    snapshot["expires_at_ms"] = 2
    snapshot["features"]["context"] = {"history": ["100"] * 10000}
    original = LegacyDirectionalPlan.read(**evidence)
    assert len(original.projection_json) < 1024
    evidence["config"]["strategy"] = "S8"
    snapshot["features"]["evaluation"]["sizing"]["stop_price"] = "1"
    snapshot["result"]["quantity"] = "100"
    assert original.protection_spec(order()).trigger_price == "92"
    with pytest.raises(FrozenInstanceError):
        original.strategy = "S8"


def test_legacy_plan_copies_account_scope_and_rejects_unknown_restored_identity():
    evidence = legacy()
    supplied = replace(SCOPE)
    evidence["scope"] = supplied
    original = LegacyDirectionalPlan.read(**evidence)
    object.__setattr__(supplied, "account_id", "changed-outside")
    assert original.scope == SCOPE
    assert original.protection_spec(order()).trigger_price == "92"
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        replace(original, strategy_version="unknown")
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        replace(original, producer="s8")


@pytest.mark.parametrize(
    "failure", ["version", "mode", "account", "environment", "producer", "strategy"]
)
def test_legacy_protection_refuses_foreign_or_unknown_identity(failure):
    evidence = legacy()
    if failure == "version":
        evidence["strategy_version"] = "strategy-v1"
    elif failure == "mode":
        evidence["config"]["mode"] = "REPLAY_ONLY"
    elif failure == "account":
        evidence["config"]["account_scope"]["account_id"] = "foreign"
    elif failure == "environment":
        evidence["config"]["account_scope"]["environment"] = "LIVE"
    elif failure == "producer":
        evidence["producer"] = "s8"
    else:
        evidence["config"]["strategy"] = "S7"
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_REQUIRED"):
        LegacyDirectionalPlan.read(**evidence)


@pytest.mark.parametrize(
    "path,value",
    [
        (("features", "evaluation", "admission_authorized"), False),
        (("features", "evaluation", "admission_authorized"), 1),
        (("features", "evaluation", "execution_market_gate"), "DENIED"),
        (("features", "evaluation", "market_plan", "strategy"), "S8"),
        (("features", "evaluation", "market_plan", "side"), "SHORT"),
        (("features", "evaluation", "sizing", "reason"), "REJECTED"),
        (("features", "evaluation", "sizing", "quantity"), "2"),
        (("result", "action"), "IGNORED"),
        (("result", "side"), "SELL"),
        (("result", "quantity"), "2"),
        (("symbol",), "ETHUSDT"),
    ],
)
def test_legacy_protection_preserves_all_original_evidence_checks(path, value):
    evidence = legacy()
    target = evidence["snapshot"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_MISMATCH"):
        LegacyDirectionalPlan.read(**evidence).protection_spec(order())


@pytest.mark.parametrize(
    "field,value", [("side", "SELL"), ("symbol", "ETHUSDT"), ("quantity", ".25")]
)
def test_stop_uses_original_requested_quantity_not_current_remaining(field, value):
    original = LegacyDirectionalPlan.read(**legacy())
    target = order()
    if field == "quantity":
        target["intent"]["quantity"] = value
    else:
        target[field] = value
    with pytest.raises(ValueError, match="DIRECTIONAL_STOP_EVIDENCE_MISMATCH"):
        original.protection_spec(target)
    # Actual position quantities are checked separately by guarded protection.
    original_order = {
        **order(),
        "remaining_quantity": ".25",
        "status": "PARTIALLY_FILLED",
    }
    assert original.protection_spec(original_order).trigger_price == "92"
