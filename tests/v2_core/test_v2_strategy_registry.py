"""Pure deployment contracts: no database fixtures or executable handlers."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.strategy import StrategyScope
from v2_core.strategy_registry import (
    MAX_DEPLOYMENT_CONFIG_BYTES,
    DeploymentBinding,
    StrategyRegistry,
    StrategySpec,
    deployment_generation,
    strategy_identifier,
)

SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")
V1 = StrategySpec("structure", "v1", "single-open-v1", "entry-v1", "exit-v1")
V2 = replace(V1, code_version="v2", entry_handler="entry-v2")


def registry(specs=(V1, V2)):
    return StrategyRegistry(
        specs,
        entry_handlers={"entry-v1", "entry-v2"},
        exit_handlers={"exit-v1", "exit-v2"},
        plan_schemas={"single-open-v1"},
    )


def binding(**changes):
    values = {
        "scope": SCOPE,
        "instance_id": "s6",
        "strategy_id": V1.strategy_id,
        "code_version": V1.code_version,
        "config": {"risk": {"fraction": "0.01", "windows": [15, 60]}},
        "generation": 1,
        "mode": "SHADOW",
    }
    return registry().bind(**{**values, **changes})


def test_consumer_keeps_existing_identity_across_code_and_generation():
    first = binding()
    changed = binding(code_version="v2", generation=2, mode="ACTIVE")
    expected = StrategyScope("BINANCE", "test-account", "SANDBOX", "FUTURES", "s6")
    assert first.consumer == changed.consumer == expected.consumer
    assert first.binding_digest != changed.binding_digest
    assert first.config_digest == changed.config_digest
    assert first.spec.identity != changed.spec.identity


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": replace(SCOPE, account_id="another")},
        {"scope": replace(SCOPE, environment="LIVE")},
        {"instance_id": "other-instance"},
    ],
)
def test_consumer_and_binding_isolate_scope_and_instance(changes):
    first, other = binding(), binding(**changes)
    assert first.consumer != other.consumer
    assert first.binding_digest != other.binding_digest


def test_registry_copies_collections_and_has_no_mutating_registration():
    specs = [V1]
    handlers = {"entry-v1"}
    catalog = StrategyRegistry(
        specs,
        entry_handlers=handlers,
        exit_handlers=["exit-v1"],
        plan_schemas=["single-open-v1"],
    )
    specs.clear()
    handlers.clear()
    assert catalog.resolve("structure", "v1") == V1
    assert catalog.entry_handlers == frozenset({"entry-v1"})
    with pytest.raises(TypeError):
        catalog.specs[V2.identity] = V2
    with pytest.raises(FrozenInstanceError):
        catalog.specs = {}
    with pytest.raises(FrozenInstanceError):
        V1.exit_handler = "exit-v2"


def test_nested_configuration_is_copied_and_canonicalized():
    config = {"risk": {"windows": [15, 60], "fraction": "0.01"}}
    frozen = binding(config=config)
    original = frozen.config_json
    original_digest = frozen.binding_digest
    config["risk"]["windows"].append(240)
    frozen.config["risk"]["windows"].append(5)
    assert frozen.config_json == original
    assert frozen.config == {"risk": {"fraction": "0.01", "windows": [15, 60]}}
    assert frozen.config_digest == digest(canonical(frozen.config))
    assert frozen.binding_digest == original_digest
    assert replace(frozen, config_json=json.dumps(frozen.config, indent=2)) == frozen
    with pytest.raises(FrozenInstanceError):
        frozen.generation = 2


@pytest.mark.parametrize("secret", ["api_key", "API-SECRET", "password", "private_key"])
def test_nested_credentials_are_rejected(secret):
    with pytest.raises(ValueError, match="credential secrets"):
        binding(config={"advisory": [{secret: "do-not-store"}]})


@pytest.mark.parametrize("config", [[], None, {"risk": 0.1}, {"risk": float("nan")}])
def test_configuration_rejects_non_objects_and_inexact_numbers(config):
    with pytest.raises((ValueError, TypeError)):
        binding(config=config)


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.0, "1", 2**53, 2**63 - 1])
def test_generation_requires_an_exact_positive_json_integer_not_bool(generation):
    with pytest.raises(ValueError, match="STRATEGY_GENERATION_INVALID"):
        binding(generation=generation)
    with pytest.raises(ValueError, match="STRATEGY_GENERATION_INVALID"):
        deployment_generation(generation)


def test_maximum_generation_and_changed_config_have_distinct_snapshots():
    first = binding()
    maximum = binding(generation=2**53 - 1)
    changed = binding(config={"risk": {"fraction": "0.02", "windows": [15, 60]}})
    assert first.consumer == maximum.consumer == changed.consumer
    assert (
        len({first.binding_digest, maximum.binding_digest, changed.binding_digest}) == 3
    )
    assert first.config_digest != changed.config_digest


@pytest.mark.parametrize("mode", ["shadow", "", "ACTIVE ", True, 1, None])
def test_mode_is_explicit(mode):
    with pytest.raises(ValueError, match="STRATEGY_MODE_INVALID"):
        binding(mode=mode)


@pytest.mark.parametrize("mode", ["SHADOW", "ACTIVE", "DRAINING", "OFF"])
def test_entry_modes_never_authorize_execution_and_do_not_disable_exits(mode):
    deployed = binding(mode=mode)
    catalog = registry()
    assert deployed.execution_authorized is False
    assert catalog.resolve_exit(deployed) == V1
    if mode in {"SHADOW", "ACTIVE"}:
        assert catalog.resolve_entry(deployed) == V1
    else:
        with pytest.raises(ValueError, match="STRATEGY_ENTRY_DISABLED"):
            catalog.resolve_entry(deployed)


def test_replacement_preserves_old_exit_resolution_but_unknown_versions_fail():
    old = binding(mode="DRAINING")
    new = binding(code_version="v2", generation=2, mode="ACTIVE")
    assert registry().resolve_exit(old).code_version == "v1"
    assert registry().resolve_entry(new).code_version == "v2"
    with pytest.raises(ValueError, match="STRATEGY_VERSION_NOT_REGISTERED"):
        registry((V2,)).resolve_exit(old)
    with pytest.raises(ValueError, match="STRATEGY_VERSION_NOT_REGISTERED"):
        binding(code_version="v3")
    with pytest.raises(ValueError, match="STRATEGY_VERSION_NOT_REGISTERED"):
        registry().resolve("unknown", "v1")


@pytest.mark.parametrize(
    "field, replacement",
    [("entry_handler", "entry-v2"), ("exit_handler", "exit-v2"), ("plan_schema", "v2")],
)
def test_same_version_cannot_substitute_handlers_or_plan_schema(field, replacement):
    forged = replace(binding(), spec=replace(V1, **{field: replacement}))
    with pytest.raises(ValueError, match="STRATEGY_SPEC_IDENTITY_CONFLICT"):
        registry().resolve_exit(forged)


@pytest.mark.parametrize("field", ["entry_handler", "exit_handler", "plan_schema"])
def test_unregistered_handler_or_schema_is_rejected(field):
    with pytest.raises(ValueError, match="STRATEGY_SPEC_NOT_WHITELISTED"):
        registry((replace(V1, **{field: "unknown"}),))


@pytest.mark.parametrize("specs", [(V1, V1), (V1, replace(V1, exit_handler="exit-v2"))])
def test_duplicate_strategy_version_is_never_overwritten(specs):
    with pytest.raises(ValueError, match="STRATEGY_DUPLICATE_VERSION"):
        registry(specs)


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        " leading",
        "trailing ",
        "a\nb",
        "a\x7fb",
        "a" * 129,
        "module:call",
        "pkg/module",
        "__import__('os')",
        True,
        None,
    ],
)
def test_identifiers_reject_ambiguous_or_executable_forms(identifier):
    with pytest.raises(ValueError):
        replace(V1, entry_handler=identifier)
    with pytest.raises(ValueError):
        binding(instance_id=identifier)


@pytest.mark.parametrize("field", ["entry_handlers", "exit_handlers", "plan_schemas"])
@pytest.mark.parametrize("value", [None, "entry-v1", [], {"entry-v1": "callable"}])
def test_registration_requires_explicit_identifier_whitelists(field, value):
    settings = {
        "entry_handlers": ["entry-v1"],
        "exit_handlers": ["exit-v1"],
        "plan_schemas": ["single-open-v1"],
    }
    with pytest.raises(ValueError, match="STRATEGY_EXPLICIT_WHITELIST_REQUIRED"):
        StrategyRegistry((V1,), **{**settings, field: value})


def test_binding_requires_typed_contracts_and_json():
    valid = binding()
    with pytest.raises(TypeError, match="STRATEGY_TYPED_SCOPE_AND_SPEC_REQUIRED"):
        replace(valid, scope={"account_id": "test-account"})
    with pytest.raises(TypeError, match="STRATEGY_TYPED_SCOPE_AND_SPEC_REQUIRED"):
        replace(valid, spec=V1.identity)
    with pytest.raises(TypeError, match="STRATEGY_CONFIG_JSON_REQUIRED"):
        replace(valid, config_json={})
    with pytest.raises(TypeError, match="STRATEGY_TYPED_BINDING_REQUIRED"):
        registry().resolve_exit({})
    assert isinstance(valid, DeploymentBinding)


def test_binding_revalidates_restored_account_fields():
    restored = replace(SCOPE)
    object.__setattr__(restored, "environment", "UNKNOWN")
    with pytest.raises(ValueError, match="explicit linear futures account scope"):
        binding(scope=restored)


def test_identifier_maximum_length_is_accepted():
    maximum = "a" * 128
    assert strategy_identifier(maximum) == maximum
    assert replace(V1, strategy_id=maximum).strategy_id == maximum
    assert binding(instance_id=maximum).instance_id == maximum


@pytest.mark.parametrize("value", [1, 2**53 - 1])
def test_public_generation_validator_preserves_accepted_values(value):
    assert deployment_generation(value) == value


@pytest.mark.parametrize(
    "encoded",
    [
        '{"risk":"0.01","risk":"0.02"}',
        '{"risk":{"fraction":"0.01","fraction":"0.02"}}',
        '{"filters":[{"threshold":"1","threshold":"2"}]}',
        '{"risk":"0.01","r\\u0069sk":"0.02"}',
    ],
)
def test_restored_configuration_rejects_duplicate_fields_at_every_level(encoded):
    with pytest.raises(ValueError, match="STRATEGY_CONFIG_DUPLICATE_FIELD"):
        replace(binding(), config_json=encoded)


def test_configuration_byte_bound_includes_input_and_canonical_form():
    overhead = len(canonical({"padding": ""}).encode("utf-8"))
    config = {"padding": "x" * (MAX_DEPLOYMENT_CONFIG_BYTES - overhead)}
    at_limit = binding(config=config)
    assert len(at_limit.config_json.encode("utf-8")) == MAX_DEPLOYMENT_CONFIG_BYTES
    with pytest.raises(ValueError, match="STRATEGY_CONFIG_BOUND"):
        binding(config={"padding": config["padding"] + "x"})
    with pytest.raises(ValueError, match="STRATEGY_CONFIG_BOUND"):
        replace(at_limit, config_json=at_limit.config_json + " ")


@pytest.mark.parametrize("count", [12000, 22000])
def test_configuration_checks_utf8_bytes_and_unicode_canonical_expansion(count):
    encoded = json.dumps({"label": "中" * count}, ensure_ascii=False)
    assert len(encoded) < MAX_DEPLOYMENT_CONFIG_BYTES
    # 12000 characters fit the incoming byte budget but exceed it after
    # canonical escaping; 22000 already exceed the UTF-8 input budget.
    with pytest.raises(ValueError, match="STRATEGY_CONFIG_BOUND"):
        replace(binding(), config_json=encoded)


@pytest.mark.parametrize(
    "encoded", ['{"nested":[{"api_key":"do-not-store"}]}', '{"risk":0.1}', "[]"]
)
def test_json_restoration_keeps_canonical_credential_and_numeric_checks(encoded):
    with pytest.raises((ValueError, TypeError)):
        replace(binding(), config_json=encoded)


def test_deployment_and_registry_own_revalidated_spec_snapshots():
    supplied = replace(V1)
    catalog = registry((supplied,))
    deployed = replace(binding(), spec=supplied)
    original_digest = deployed.binding_digest
    assert deployed.spec is not supplied
    assert catalog.resolve(*V1.identity) is not supplied

    # Model a caller restoring/changing its own dataclass through a low-level
    # mechanism. Copies must not rewrite an already published deployment.
    object.__setattr__(supplied, "exit_handler", "exit-v2")
    assert deployed.spec == V1
    assert deployed.binding_digest == original_digest
    assert catalog.resolve(*V1.identity) == V1
    assert catalog.resolve_exit(deployed) == V1


@pytest.mark.parametrize(
    "field,value", [("entry_handler", "pkg:execute"), ("code_version", True)]
)
def test_restored_spec_fields_are_revalidated_before_binding_or_registration(
    field, value
):
    restored = replace(V1)
    object.__setattr__(restored, field, value)
    with pytest.raises(ValueError):
        registry((restored,))
    with pytest.raises(ValueError):
        replace(binding(), spec=restored)
