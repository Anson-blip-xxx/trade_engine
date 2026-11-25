"""Isolated-PG deployment CAS and entry fencing; never contacts a venue."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, replace
from threading import Event
from time import time_ns
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountRiskDenied, AccountScope
from v2_core.evidence import DecisionEvidence, canonical
from v2_core.intents import AdmissionCode, OpenIntent
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import (
    NAMESPACE,
    STRATEGY_ENTRY_DENIALS,
    StrategyDeployments,
    deployment_snapshot,
    require_strategy_opening,
)
from v2_core.strategy_plan import PLAN_SCHEMA, TradePlan
from v2_core.strategy_registry import StrategyRegistry, StrategySpec

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")
V1 = StrategySpec("structure", "v1", "single-open-v1", "entry-v1", "exit-v1")
V2 = replace(V1, code_version="v2", entry_handler="entry-v2")
_DEFAULT = object()


def registry():
    return StrategyRegistry(
        [V1, V2],
        entry_handlers=["entry-v1", "entry-v2"],
        exit_handlers=["exit-v1"],
        plan_schemas=["single-open-v1"],
    )


def binding(**changes):
    return registry().bind(
        **{
            "scope": SCOPE,
            "instance_id": "structure-main",
            "strategy_id": "structure",
            "code_version": "v1",
            "config": {"risk_fraction": "0.01"},
            "generation": 1,
            "mode": "ACTIVE",
            **changes,
        }
    )


def publish(database, selected=None, request_id=None):
    selected = binding() if selected is None else selected
    return StrategyDeployments(database, registry()).publish(
        selected,
        expected_generation=selected.generation - 1,
        request_id=request_id or str(uuid4()),
    )


def admit(database, selected=None, *, proof=_DEFAULT, **changes):
    selected = binding() if selected is None else selected
    scope = changes.pop("scope", selected.scope)
    config = changes.pop("config", selected.config)
    code_version = changes.pop("code_version", selected.spec.code_version)
    producer = changes.pop("producer", selected.instance_id)
    snapshot = {
        "observed_at": 1,
        "decided_at": 2,
        "expires_at_ms": 100000,
        "source": "test",
        "symbol": "BTCUSDT",
        "rationale": "isolated QA",
        "features": {},
        **changes,
    }
    if proof is _DEFAULT:
        snapshot["strategy_deployment"] = deployment_snapshot(selected)
    elif proof is not None:
        snapshot["strategy_deployment"] = proof
    evidence = DecisionEvidence(code_version, canonical(config), canonical(snapshot))
    intent = OpenIntent(
        str(uuid4()),
        scope.exchange,
        scope.account_id,
        scope.environment,
        scope.product,
        producer,
        str(uuid4()),
        "BTCUSDT",
        "BUY",
        "0.01",
        code_version,
        evidence.config_digest,
        evidence.evidence_ref,
    )
    assert TradingData(database).accept(intent, evidence).code is AdmissionCode.ACCEPTED
    return intent


def check(database, intent):
    with database() as conn:
        conn.execute(
            "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
            (intent.intent_id,),
        )
        return require_strategy_opening(conn, intent.intent_id)


def test_snapshot_is_detached_and_omits_configuration_content():
    selected = binding()
    snapshot = deployment_snapshot(selected)
    assert set(snapshot) == {
        "scope",
        "instance_id",
        "spec",
        "config_digest",
        "generation",
        "mode",
        "binding_digest",
    }
    assert snapshot["config_digest"] == selected.config_digest
    snapshot["scope"]["account_id"] = "mutated"
    snapshot["spec"]["entry_handler"] = "mutated"
    assert selected.scope == SCOPE and selected.spec == V1
    assert selected.execution_authorized is False


@pytest.mark.parametrize("expected", [True, -1, 1, "0", 2**53 - 1])
def test_publish_validates_next_generation_without_io(expected):
    store = StrategyDeployments(
        lambda: pytest.fail("unexpected database I/O"), registry()
    )
    with pytest.raises(ValueError, match="NEXT_GENERATION"):
        store.publish(binding(), expected_generation=expected, request_id="q1")


def test_publish_rejects_unregistered_spec_without_io():
    selected = replace(binding(), spec=replace(V1, exit_handler="unknown"))
    store = StrategyDeployments(
        lambda: pytest.fail("unexpected database I/O"), registry()
    )
    with pytest.raises(ValueError, match="SPEC_IDENTITY_CONFLICT"):
        store.publish(selected, expected_generation=0, request_id="q1")


def test_read_unregistered_returns_none_and_active_fence_requires_durable_publication(
    database,
):
    store = StrategyDeployments(database, registry())
    assert store.read(SCOPE, binding().instance_id) is None
    original = admit(database)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISSING"):
        check(database, original)
    publish(database)
    assert check(database, original) == deployment_snapshot(binding())
    assert store.read(SCOPE, binding().instance_id) == binding()


def test_publication_and_old_request_replay_are_idempotent_without_reactivation(
    database,
):
    store = StrategyDeployments(database, registry())
    first = binding()
    kwargs = {"expected_generation": 0, "request_id": "first"}
    applied = store.publish(first, **kwargs)
    assert applied == {
        "status": "APPLIED",
        "generation": 1,
        "binding_digest": first.binding_digest,
        "execution_authorized": False,
    }
    assert store.publish(first, **kwargs) == {**applied, "status": "ALREADY_APPLIED"}
    second = replace(first, generation=2, mode="DRAINING")
    assert publish(database, second)["status"] == "APPLIED"
    assert store.publish(first, **kwargs) == {**applied, "status": "ALREADY_APPLIED"}
    assert store.read(SCOPE, first.instance_id) == second
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone() == (2,)


def test_stale_generation_does_not_overwrite_current_and_request_conflicts(database):
    store = StrategyDeployments(database, registry())
    first = binding()
    publish(database, first, request_id="first")
    with pytest.raises(ValueError, match="state request content conflict"):
        store.publish(
            replace(first, mode="OFF"), expected_generation=0, request_id="first"
        )
    second = replace(first, generation=2, mode="DRAINING")
    publish(database, second)
    result = store.publish(first, expected_generation=0, request_id="different")
    assert result == {
        "status": "STALE",
        "generation": 2,
        "binding_digest": second.binding_digest,
        "execution_authorized": False,
    }
    assert store.read(SCOPE, first.instance_id) == second


def test_missing_expected_generation_cannot_create_a_noninitial_state(database):
    result = publish(database, binding(generation=2))
    assert result["status"] == "STALE" and result["generation"] == 0
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_business_state").fetchone() == (0,)


@pytest.mark.parametrize("mode", ["SHADOW", "DRAINING", "OFF"])
def test_nonactive_modes_never_authorize_open(database, mode):
    selected = binding(mode=mode)
    publish(database, selected)
    original = admit(database, selected)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_DISABLED"):
        check(database, original)


def test_resume_creates_a_new_generation_and_never_revives_old_plan(database):
    first = binding()
    publish(database, first)
    old = admit(database, first)
    publish(database, replace(first, generation=2, mode="DRAINING"))
    resumed = replace(first, generation=3)
    publish(database, resumed)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISMATCH"):
        check(database, old)
    assert check(database, admit(database, resumed)) == deployment_snapshot(resumed)


@pytest.mark.parametrize(
    "changes", [{"code_version": "v2"}, {"config": {"risk_fraction": "0.005"}}]
)
def test_code_and_configuration_change_fence_cached_generation(database, changes):
    first = binding()
    publish(database, first)
    old = admit(database, first)
    second = binding(generation=2, **changes)
    publish(database, second)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISMATCH"):
        check(database, old)
    assert check(database, admit(database, second)) == deployment_snapshot(second)


def test_legacy_without_a_binding_remains_compatible_but_managed_missing_proof_does_not(
    database,
):
    legacy = admit(database, proof=None)
    assert check(database, legacy) is None
    publish(database)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_REQUIRED"):
        check(database, legacy)
    assert (
        check(database, admit(database, proof=None, producer="legacy-unmanaged"))
        is None
    )


def test_new_plan_without_deployment_proof_cannot_fall_back_to_legacy(database):
    original = admit(
        database, proof=None, strategy_plan={"schema_version": "strategy-trade-plan-v1"}
    )
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_REQUIRED"):
        check(database, original)


@pytest.mark.parametrize("mixed_plan", [None, {}, {"schema_version": PLAN_SCHEMA}])
def test_new_plan_marker_cannot_bypass_validation_using_a_legacy_schema(
    database, mixed_plan
):
    publish(database)
    original = admit(database, strategy_plan=mixed_plan)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_INVALID"):
        check(database, original)


def plan_binding(database):
    spec = replace(V1, plan_schema=PLAN_SCHEMA)
    selected = replace(binding(), spec=spec)
    catalog = StrategyRegistry(
        [spec],
        entry_handlers=[spec.entry_handler],
        exit_handlers=[spec.exit_handler],
        plan_schemas=[PLAN_SCHEMA],
    )
    StrategyDeployments(database, catalog).publish(
        selected, expected_generation=0, request_id="new-plan-binding"
    )
    now = time_ns() // 1000000
    plan = TradePlan(
        scope=SCOPE,
        instance_id=selected.instance_id,
        strategy_id=spec.strategy_id,
        code_version=spec.code_version,
        deployment_generation=1,
        config_digest=selected.config_digest,
        opportunity_id="qa-opportunity",
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.01",
        entry_price="100",
        stop_price="98",
        planned_loss="0.03",
        quantity_step="0.001",
        leverage=2,
        margin_mode="ISOLATED",
        observed_at_ms=now - 1000,
        decided_at_ms=now - 500,
        expires_at_ms=now + 60000,
        exit_handler=spec.exit_handler,
        exit_version="exit-v1",
        exit_config_json="{}",
    )
    return selected, plan


def admit_plan(database, selected, plan):
    return admit(
        database,
        selected,
        strategy_plan=plan.payload(),
        observed_at=plan.observed_at_ms,
        decided_at=plan.decided_at_ms,
        expires_at_ms=plan.expires_at_ms,
    )


def test_new_schema_binding_without_a_complete_trade_plan_is_rejected(database):
    selected, _ = plan_binding(database)
    original = admit(database, selected)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_INVALID"):
        check(database, original)


def test_new_schema_complete_plan_passes_without_requiring_exit_version_equal_code_version(
    database,
):
    selected, plan = plan_binding(database)
    assert plan.exit_version != plan.code_version
    assert check(database, admit_plan(database, selected, plan)) == deployment_snapshot(
        selected
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": "another-family"},
        {"deployment_generation": 2},
        {"exit_handler": "unbound-exit"},
        {"code_version": "v2"},
        {"config_digest": "0" * 64},
    ],
)
def test_new_schema_plan_cannot_differ_from_frozen_deployment(database, changes):
    selected, plan = plan_binding(database)
    original = admit_plan(database, selected, replace(plan, **changes))
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_INVALID"):
        check(database, original)


@pytest.mark.parametrize("future", [False, True])
def test_new_schema_plan_entry_lifetime_uses_database_clock(database, future):
    selected, plan = plan_binding(database)
    now = time_ns() // 1000000
    if future:
        plan = replace(
            plan,
            observed_at_ms=now + 60000,
            decided_at_ms=now + 60000,
            expires_at_ms=now + 120000,
        )
    else:
        plan = replace(plan, observed_at_ms=1, decided_at_ms=2, expires_at_ms=3)
    original = admit_plan(database, selected, plan)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_EXPIRED"):
        check(database, original)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": replace(SCOPE, account_id="other")},
        {"scope": replace(SCOPE, environment="LIVE")},
        {"producer": "another-instance"},
        {"code_version": "v2"},
    ],
)
def test_frozen_proof_cannot_be_rebound_to_another_intent_scope_or_version(
    database, changes
):
    publish(database)
    original = admit(database, **changes)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_MISMATCH"):
        check(database, original)


def test_evidence_configuration_cannot_differ_from_the_frozen_digest(database):
    publish(database)
    original = admit(database, config={"risk_fraction": "0.02"})
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_INVALID"):
        check(database, original)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(binding_digest="0" * 64),
        lambda p: p.update(config_digest="0" * 64),
        lambda p: p.update(generation=True),
        lambda p: p.update(unknown="extra"),
        lambda p: p.pop("spec"),
        lambda p: p["spec"].update(entry_handler="unregistered"),
    ],
)
def test_malformed_frozen_proof_is_a_fixed_entry_denial(database, mutate):
    publish(database)
    proof = deployment_snapshot(binding())
    mutate(proof)
    original = admit(database, proof=proof)
    with pytest.raises(AccountRiskDenied) as caught:
        check(database, original)
    assert str(caught.value) == "STRATEGY_DEPLOYMENT_PROOF_INVALID"
    assert str(caught.value) in STRATEGY_ENTRY_DENIALS


@pytest.mark.parametrize("deleted", [False, True])
def test_corrupt_state_or_tombstone_never_enables_legacy_fallback(database, deleted):
    first = binding()
    publish(database, first)
    original = admit(database, proof=None)
    key = StateKey(**asdict(SCOPE), namespace=NAMESPACE, key=first.instance_id)
    assert (
        BusinessState(database)
        .change(
            key,
            expected_version=1,
            request_key="invalid",
            payload={} if deleted else {"broken": True},
            reason="invalid",
            deleted=deleted,
        )
        .code
        == "APPLIED"
    )
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_INVALID"):
        check(database, original)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_INVALID"):
        StrategyDeployments(database, registry()).read(SCOPE, first.instance_id)
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_INVALID"):
        publish(database, replace(first, generation=3))


def test_publication_rolls_back_with_the_callers_transaction(database):
    with pytest.raises(RuntimeError, match="rollback"), database() as conn:
        publish(lambda: nullcontext(conn))
        raise RuntimeError("rollback")
    assert (
        StrategyDeployments(database, registry()).read(SCOPE, binding().instance_id)
        is None
    )
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_state_history").fetchone() == (0,)


def test_entry_fence_uses_supplied_connection_and_holds_lock_until_commit(database):
    first = binding()
    publish(database, first)
    original = admit(database, first)
    started = Event()

    def pause():
        started.set()
        return publish(database, replace(first, generation=2, mode="DRAINING"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database() as conn:
            conn.execute(
                "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (original.intent_id,),
            )
            assert require_strategy_opening(
                conn, original.intent_id
            ) == deployment_snapshot(first)
            future = pool.submit(pause)
            assert started.wait(3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=5)["status"] == "APPLIED"
    with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_DISABLED"):
        check(database, original)


def test_publication_wins_race_before_entry_check(database):
    first = binding()
    publish(database, first)
    original = admit(database, first)
    started = Event()

    def validate():
        started.set()
        return check(database, original)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database() as conn:
            publish(lambda: nullcontext(conn), replace(first, generation=2, mode="OFF"))
            future = pool.submit(validate)
            assert started.wait(3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        with pytest.raises(AccountRiskDenied, match="STRATEGY_ENTRY_DISABLED"):
            future.result(timeout=5)


def test_first_publication_is_serialized_even_before_the_state_row_exists(database):
    original = admit(database, proof=None)
    started = Event()

    def first_publish():
        started.set()
        return publish(database)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database() as conn:
            conn.execute(
                "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (original.intent_id,),
            )
            assert require_strategy_opening(conn, original.intent_id) is None
            future = pool.submit(first_publish)
            assert started.wait(3)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=5)["status"] == "APPLIED"
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_PROOF_REQUIRED"):
        check(database, original)


def test_distinct_accounts_and_instances_have_independent_generation_histories(
    database,
):
    selections = [
        binding(),
        binding(instance_id="other"),
        binding(scope=replace(SCOPE, account_id="other")),
    ]
    for selected in selections:
        assert publish(database, selected, request_id="same-request")["generation"] == 1
        assert check(database, admit(database, selected)) == deployment_snapshot(
            selected
        )
    publish(database, replace(selections[0], generation=2, mode="OFF"))
    for selected in selections[1:]:
        assert check(database, admit(database, selected)) == deployment_snapshot(
            selected
        )


def test_missing_current_with_retained_history_fails_closed_without_another_connection():
    """Schema FKs prevent this normally; broken/restored state still fails closed."""
    from v2_core.strategy_deployment import _read_current

    class Query:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, statement, _params):
            if "FROM v2_business_state" in statement:
                return Query(None)
            assert "FROM v2_state_history" in statement
            return Query((1,))

    key = StateKey(**asdict(SCOPE), namespace=NAMESPACE, key="structure-main")
    with pytest.raises(AccountRiskDenied, match="STRATEGY_DEPLOYMENT_INVALID"):
        _read_current(Connection(), key)
