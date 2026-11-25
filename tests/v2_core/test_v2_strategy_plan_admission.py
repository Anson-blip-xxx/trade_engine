"""Pure contracts and isolated-PG atomic admission, without any venue I/O."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from threading import Barrier
from uuid import uuid4

import pytest

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountRiskDenied, AccountScope
from v2_core.intents import Admission, AdmissionCode, IntentStore, OpenIntent
from v2_core.service import TradingData
from v2_core.signals import Signals
from v2_core.state import BusinessState
from v2_core.strategy_deployment import StrategyDeployments, deployment_snapshot
from v2_core.strategy_plan import PLAN_SCHEMA, TradePlan
from v2_core.strategy_plan_admission import (
    NAMESPACE,
    StrategyPlanAdmission,
    StrategyPlanAdmissionError,
    opportunity_intent_id,
    opportunity_key,
    require_plan_admission,
)
from v2_core.strategy_registry import StrategyRegistry, StrategySpec

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-plan-admission", "SANDBOX", "FUTURES")


def contracts(*, now=1000, scope=SCOPE, instance="trend-a", mode="ACTIVE"):
    spec = StrategySpec(
        "breakout", "entry-v1", PLAN_SCHEMA, "breakout-entry", "trend-exit"
    )
    registry = StrategyRegistry(
        [spec, replace(spec, code_version="entry-v2")],
        entry_handlers=["breakout-entry"],
        exit_handlers=["trend-exit"],
        plan_schemas=[PLAN_SCHEMA],
    )
    binding = registry.bind(
        scope=scope,
        instance_id=instance,
        strategy_id=spec.strategy_id,
        code_version=spec.code_version,
        config={"risk": "0.01"},
        generation=1,
        mode=mode,
    )
    plan = TradePlan(
        scope=scope,
        instance_id=instance,
        strategy_id=spec.strategy_id,
        code_version=spec.code_version,
        deployment_generation=1,
        config_digest=binding.config_digest,
        opportunity_id="BTCUSDT:15:closed-structure-1",
        symbol="BTCUSDT",
        side="BUY",
        quantity="1.25",
        entry_price="100",
        stop_price="92",
        planned_loss="10.5",
        quantity_step="0.0025",
        leverage=2,
        margin_mode="ISOLATED",
        observed_at_ms=now - 100,
        decided_at_ms=now - 50,
        expires_at_ms=now + 60000,
        exit_handler="trend-exit",
        exit_version="exit-v1",
        exit_config_json='{"atr":"3"}',
    )
    return registry, binding, plan


def setup(database, **kwargs):
    with database() as conn:
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
    registry, binding, plan = contracts(now=now, **kwargs)
    assert (
        StrategyDeployments(database, registry).publish(
            binding, expected_generation=0, request_id="qa-first"
        )["status"]
        == "APPLIED"
    )
    return StrategyPlanAdmission(database, registry), registry, binding, plan


def counts(database):
    with database() as conn:
        return conn.execute(
            """SELECT (SELECT count(*) FROM v2_trade_intents),
            (SELECT count(*) FROM v2_domain_outbox),
            (SELECT count(*) FROM v2_decision_evidence),
            (SELECT count(*) FROM v2_signal_receipts),
            (SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'=%s),
            (SELECT count(*) FROM v2_orders), (SELECT count(*) FROM v2_risk_reservations)""",
            (NAMESPACE,),
        ).fetchone()


def signal(database, plan, *, source="tv_bridge", **changes):
    return Signals(database).admit(
        source=source,
        environment=plan.scope.environment,
        request_key=str(uuid4()),
        snapshot={
            "observed_at": plan.observed_at_ms,
            "expires_at_ms": plan.expires_at_ms,
            "symbol": plan.symbol,
            "signal": "TREND_UP_LONG",
            "features": {},
            **changes,
        },
    )


def test_opportunity_identity_is_account_bound_not_instance_version_or_signal():
    _, binding, plan = contracts()
    original = opportunity_intent_id(plan)
    assert opportunity_intent_id(replace(plan, instance_id="trend-b")) == original
    assert opportunity_intent_id(replace(plan, code_version="entry-v2")) == original
    assert opportunity_intent_id(replace(plan, deployment_generation=2)) == original
    assert (
        opportunity_intent_id(replace(plan, side="SELL", stop_price="108")) == original
    )
    assert (
        opportunity_intent_id(replace(plan, scope=replace(SCOPE, account_id="other")))
        != original
    )
    assert (
        opportunity_intent_id(replace(plan, opportunity_id="next-structure"))
        != original
    )
    assert binding.consumer.endswith('"trend-a"]')


@pytest.mark.parametrize(
    "change",
    [
        {"scope": replace(SCOPE, account_id="other")},
        {"instance_id": "trend-b"},
        {"strategy_id": "other-strategy"},
        {"code_version": "entry-v2"},
        {"deployment_generation": 2},
        {"config_digest": "f" * 64},
        {"exit_handler": "other-exit"},
    ],
)
def test_binding_mismatch_rejected_before_database(change):
    registry, binding, plan = contracts()
    adapter = StrategyPlanAdmission(lambda: pytest.fail("must not connect"), registry)
    with pytest.raises(StrategyPlanAdmissionError, match="BINDING_MISMATCH"):
        adapter.admit(replace(plan, **change), binding, rationale="QA")


@pytest.mark.parametrize("mode", ["SHADOW", "OFF", "DRAINING"])
def test_non_active_binding_has_no_admission_authority(mode):
    registry, binding, plan = contracts(mode=mode)
    adapter = StrategyPlanAdmission(lambda: pytest.fail("must not connect"), registry)
    with pytest.raises(ValueError):
        adapter.admit(plan, binding, rationale="QA")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"features": {"secret": "not-business-data"}},
        {"features": {"price": 1.5}},
        {"features": {"large": "x" * 65536}},
        {"features": []},
        {"rationale": ""},
        {"rationale": "x" * 2049},
        {"signal_id": "bad-id"},
    ],
)
def test_invalid_features_and_reason_rejected_before_database(kwargs):
    registry, binding, plan = contracts()
    adapter = StrategyPlanAdmission(lambda: pytest.fail("must not connect"), registry)
    with pytest.raises((ValueError, TypeError)):
        adapter.admit(plan, binding, **{"rationale": "QA", **kwargs})


def test_atomic_admission_persists_frozen_plan_without_orders_or_risk(database):
    adapter, _, binding, plan = setup(database)
    features = {"input_digest": "a" * 64}
    result = adapter.admit(
        plan, binding, rationale="closed structure", features=features
    )
    features["input_digest"] = "changed-after-call"
    assert result["status"] == "ACCEPTED"
    assert result["execution_authorized"] is False
    assert counts(database) == (1, 1, 2, 0, 1, 0, 0)
    trace = TradingData(database).trace(result["intent_id"])
    assert trace["status"] == "RECEIVED"
    assert trace["episode"] is None and trace["orders"] == []
    assert trace["configuration"] == binding.config
    assert trace["decision"]["strategy_plan"] == plan.payload()
    assert trace["decision"]["strategy_deployment"] == deployment_snapshot(binding)
    assert trace["decision"]["features"] == {"input_digest": "a" * 64}
    assert trace["scope"]["producer"] == binding.instance_id
    assert trace["request"]["strategy_version"] == plan.code_version


def test_exact_replay_does_not_refresh_or_reauthorize_after_stop(database):
    adapter, registry, binding, plan = setup(database)
    first = adapter.admit(plan, binding, rationale="QA")
    StrategyDeployments(database, registry).publish(
        replace(binding, mode="DRAINING", generation=2),
        expected_generation=1,
        request_id="qa-stop",
    )
    before = counts(database)
    replay = adapter.admit(plan, binding, rationale="QA")
    assert replay == {**first, "status": "ALREADY_ACCEPTED"}
    assert counts(database) == before
    assert not replay["execution_authorized"]


@pytest.mark.parametrize(
    "change",
    [
        {"quantity": "1"},
        {"exit_version": "exit-v2"},
        {"opportunity_id": "BTCUSDT:15:closed-structure-1", "stop_price": "93"},
    ],
)
def test_same_opportunity_changed_frozen_plan_is_not_reinterpreted(database, change):
    adapter, _, binding, plan = setup(database)
    adapter.admit(plan, binding, rationale="QA")
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="FROZEN_CONTENT_CONFLICT"):
        adapter.admit(replace(plan, **change), binding, rationale="QA")
    assert counts(database) == before


@pytest.mark.parametrize(
    "changed", [{"rationale": "different"}, {"features": {"extra": True}}]
)
def test_same_plan_changed_decision_evidence_is_a_conflict(database, changed):
    adapter, _, binding, plan = setup(database)
    adapter.admit(plan, binding, rationale="QA")
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="FROZEN_CONTENT_CONFLICT"):
        adapter.admit(plan, binding, **{"rationale": "QA", **changed})
    assert counts(database) == before


@pytest.mark.parametrize("offset", [-120000, 120000])
def test_database_clock_rejects_expired_and_future_plans_atomically(database, offset):
    adapter, _, binding, plan = setup(database)
    shifted = replace(
        plan,
        observed_at_ms=plan.observed_at_ms + offset,
        decided_at_ms=plan.decided_at_ms + offset,
        expires_at_ms=plan.expires_at_ms + offset,
    )
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="NOT_CURRENT"):
        adapter.admit(shifted, binding, rationale="QA")
    assert counts(database) == before


@pytest.mark.parametrize("mode", ["DRAINING", "OFF", "SHADOW", "ACTIVE"])
def test_current_database_generation_and_mode_must_match(database, mode):
    adapter, registry, binding, plan = setup(database)
    StrategyDeployments(database, registry).publish(
        replace(binding, generation=2, mode=mode),
        expected_generation=1,
        request_id="qa-switch",
    )
    before = counts(database)
    with pytest.raises(AccountRiskDenied):
        adapter.admit(plan, binding, rationale="QA")
    assert counts(database) == before


def test_missing_database_deployment_is_not_implicit_activation(database):
    adapter, _, binding, plan = setup(database)
    other_scope = replace(SCOPE, account_id="not-published")
    before = counts(database)
    with pytest.raises(AccountRiskDenied, match="MISSING"):
        adapter.admit(
            replace(plan, scope=other_scope),
            replace(binding, scope=other_scope),
            rationale="QA",
        )
    assert counts(database) == before


def test_account_isolation_allows_same_opportunity_independently(database):
    adapter, _, binding, plan = setup(database)
    other, _, other_binding, other_plan = setup(
        database, scope=replace(SCOPE, account_id="other")
    )
    first = adapter.admit(plan, binding, rationale="QA")
    second = other.admit(other_plan, other_binding, rationale="QA")
    assert first["intent_id"] != second["intent_id"]
    assert counts(database)[0] == counts(database)[4] == 2


def test_cross_instance_concurrent_claim_has_one_permanent_winner(database):
    adapter, _, binding, plan = setup(database)
    other, _, other_binding, other_plan = setup(database, instance="trend-b")
    barrier = Barrier(2)

    def run(args):
        port, selected, candidate = args
        barrier.wait(timeout=5)
        try:
            return port.admit(candidate, selected, rationale="QA")["status"]
        except StrategyPlanAdmissionError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                run, [(adapter, binding, plan), (other, other_binding, other_plan)]
            )
        )
    assert sorted(results) == ["ACCEPTED", "REJECTED"]
    assert counts(database) == (1, 1, 2, 0, 1, 0, 0)


def test_concurrent_identical_retries_return_one_immutable_intent(database):
    adapter, _, binding, plan = setup(database)
    barrier = Barrier(4)

    def run(_):
        barrier.wait(timeout=5)
        return adapter.admit(plan, binding, rationale="QA")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert [r["status"] for r in results].count("ACCEPTED") == 1
    assert [r["status"] for r in results].count("ALREADY_ACCEPTED") == 3
    assert len({r["intent_id"] for r in results}) == 1
    assert counts(database) == (1, 1, 2, 0, 1, 0, 0)


def test_signal_receipt_and_intent_share_the_opportunity_transaction(database):
    adapter, _, binding, plan = setup(database)
    identity = signal(database, plan)
    result = adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    assert result["status"] == "ACCEPTED"
    assert counts(database) == (1, 1, 2, 1, 1, 0, 0)
    trace = TradingData(database).trace(result["intent_id"])
    assert trace["decision"]["source"] == "tv_bridge"
    assert trace["decision"]["signal_id"] == identity
    assert trace["scope"]["request_key"] == "signal:" + identity
    assert (
        adapter.admit(plan, binding, rationale="QA", signal_id=identity)["status"]
        == "ALREADY_ACCEPTED"
    )


def test_second_signal_cannot_reopen_the_same_opportunity(database):
    adapter, _, binding, plan = setup(database)
    first, second = signal(database, plan), signal(database, plan, source="s3")
    adapter.admit(plan, binding, rationale="QA", signal_id=first)
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="ALREADY_CLAIMED"):
        adapter.admit(plan, binding, rationale="QA", signal_id=second)
    assert counts(database) == before


def test_preexisting_ignored_signal_receipt_rolls_back_all_new_admission(database):
    adapter, _, binding, plan = setup(database)
    identity = signal(database, plan)
    Signals(database).complete(
        consumer=binding.consumer,
        signal_id=identity,
        outcome="IGNORED",
        reason="QA ignored",
    )
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="INTENT_CONFLICT"):
        adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    assert counts(database) == before


def test_deployment_stop_after_signal_registration_rolls_back_signal_receipt(database):
    adapter, registry, binding, plan = setup(database)
    identity = signal(database, plan)
    StrategyDeployments(database, registry).publish(
        replace(binding, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-stop-signal",
    )
    before = counts(database)
    with pytest.raises(AccountRiskDenied, match="ENTRY_DISABLED"):
        adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    assert counts(database) == before


def test_one_signal_cannot_be_reassigned_to_a_new_opportunity(database):
    adapter, _, binding, plan = setup(database)
    identity = signal(database, plan)
    adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="INTENT_CONFLICT"):
        adapter.admit(
            replace(plan, opportunity_id="different-structure"),
            binding,
            rationale="QA",
            signal_id=identity,
        )
    assert counts(database) == before


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"expires_at_ms": 2000},
        {"observed_at": 9999999999999, "expires_at_ms": 99999999999999},
    ],
)
def test_signal_cannot_change_symbol_or_extend_deadline(database, change):
    adapter, _, binding, plan = setup(database)
    if change.get("expires_at_ms") == 2000:
        change = {"observed_at": 1000, "expires_at_ms": 2000}
    identity = signal(database, plan, **change)
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="SIGNAL_MISMATCH"):
        adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    assert counts(database) == before


def test_middle_receipt_failure_rolls_back_intent_evidence_and_signal(
    database, monkeypatch
):
    adapter, _, binding, plan = setup(database)
    identity = signal(database, plan)
    before = counts(database)
    original = BusinessState.change

    def fail(self, key, **kwargs):
        if key.namespace == NAMESPACE:
            raise RuntimeError("QA_RECEIPT_FAILURE")
        return original(self, key, **kwargs)

    monkeypatch.setattr(BusinessState, "change", fail)
    with pytest.raises(RuntimeError, match="QA_RECEIPT_FAILURE"):
        adapter.admit(plan, binding, rationale="QA", signal_id=identity)
    assert counts(database) == before


def test_intent_unknown_must_not_commit_even_if_underlying_insert_completed(
    database, monkeypatch
):
    adapter, _, binding, plan = setup(database)
    before = counts(database)
    original = IntentStore.admit

    def unknown(self, intent):
        assert original(self, intent).code is AdmissionCode.ACCEPTED
        return Admission(AdmissionCode.UNKNOWN)

    monkeypatch.setattr(IntentStore, "admit", unknown)
    with pytest.raises(StrategyPlanAdmissionError, match="OUTCOME_UNKNOWN"):
        adapter.admit(plan, binding, rationale="QA")
    assert counts(database) == before


def test_outer_commit_failure_does_not_return_success_or_keep_receipts(database):
    _, registry, binding, plan = setup(database)
    identity = signal(database, plan)
    before = counts(database)

    @contextmanager
    def fail_commit():
        with database() as conn:
            yield conn
            raise RuntimeError("QA_COMMIT_FAILURE")

    with pytest.raises(RuntimeError, match="QA_COMMIT_FAILURE"):
        StrategyPlanAdmission(fail_commit, registry).admit(
            plan, binding, rationale="QA", signal_id=identity
        )
    assert counts(database) == before


def test_lost_commit_ack_is_recovered_by_exact_read_only_retry(database):
    adapter, registry, binding, plan = setup(database)

    @contextmanager
    def lose_ack():
        with database() as conn:
            yield conn
        raise RuntimeError("QA_ACK_LOST_AFTER_COMMIT")

    with pytest.raises(RuntimeError, match="QA_ACK_LOST_AFTER_COMMIT"):
        StrategyPlanAdmission(lose_ack, registry).admit(plan, binding, rationale="QA")
    before = counts(database)
    assert adapter.admit(plan, binding, rationale="QA")["status"] == "ALREADY_ACCEPTED"
    assert counts(database) == before == (1, 1, 2, 0, 1, 0, 0)


def test_settlement_does_not_release_opportunity_execution_identity(database):
    adapter, registry, binding, plan = setup(database)
    result = adapter.admit(plan, binding, rationale="QA")
    # Isolated synthetic terminal episode, not a claimed venue fill or profit.
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key,status) VALUES (%s,%s,'SETTLED')",
            (result["intent_id"], "qa-terminal"),
        )
    next_binding = replace(binding, generation=2)
    StrategyDeployments(database, registry).publish(
        next_binding, expected_generation=1, request_id="qa-next"
    )
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="FROZEN_CONTENT_CONFLICT"):
        adapter.admit(
            replace(plan, deployment_generation=2), next_binding, rationale="QA"
        )
    assert counts(database) == before
    assert adapter.admit(plan, binding, rationale="QA")["status"] == "ALREADY_ACCEPTED"


def test_deleted_receipt_is_not_an_unclaimed_opportunity(database):
    adapter, _, binding, plan = setup(database)
    adapter.admit(plan, binding, rationale="QA")
    store = BusinessState(database)
    assert (
        store.change(
            opportunity_key(plan),
            expected_version=1,
            request_key="qa-corrupt-tombstone",
            payload={},
            reason="QA",
            deleted=True,
        ).code
        == "APPLIED"
    )
    before = counts(database)
    with pytest.raises(StrategyPlanAdmissionError, match="RECEIPT_INVALID"):
        adapter.admit(plan, binding, rationale="QA")
    assert counts(database) == before


def generic_intent_without_claim(database, *, random_identity=False, with_signal=False):
    """Exercise the generic data layer, which is not opportunity authorization."""
    adapter, registry, binding, plan = setup(database)
    signal_id = signal(database, plan) if with_signal else None
    with database() as conn:
        proof = adapter._proof(conn, plan, binding, "QA", {}, signal_id)
    episode = str(uuid4()) if random_identity else opportunity_intent_id(plan)
    intent = OpenIntent(
        episode,
        **asdict(plan.scope),
        producer=plan.instance_id,
        request_key="qa-generic:" + episode,
        symbol=plan.symbol,
        side=plan.side,
        quantity=plan.quantity,
        strategy_version=plan.code_version,
        config_digest=proof.config_digest,
        evidence_ref=proof.evidence_ref,
    )
    assert TradingData(database).accept(intent, proof).code is AdmissionCode.ACCEPTED
    expected = adapter._receipt(plan, binding, proof, signal_id)
    return registry, binding, plan, episode, expected


@pytest.mark.parametrize("with_signal", [False, True])
def test_dispatch_claim_helper_accepts_exact_frozen_history_without_authority(
    database, with_signal
):
    adapter, registry, binding, plan = setup(database)
    result = adapter.admit(
        plan,
        binding,
        rationale="QA",
        signal_id=signal(database, plan) if with_signal else None,
    )
    StrategyDeployments(database, registry).publish(
        replace(binding, mode="OFF", generation=2),
        expected_generation=1,
        request_id="qa-off-before-history-read",
    )
    before = counts(database)
    with database() as conn:
        receipt = require_plan_admission(
            conn, result["intent_id"], deployment=deployment_snapshot(binding)
        )
    assert receipt["intent_id"] == result["intent_id"]
    assert receipt["execution_authorized"] is False
    assert counts(database) == before


@pytest.mark.parametrize("random_identity", [False, True])
def test_generic_intent_cannot_bypass_opportunity_claim(database, random_identity):
    _, binding, _, episode, _ = generic_intent_without_claim(
        database, random_identity=random_identity
    )
    before = counts(database)
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(conn, episode, deployment=deployment_snapshot(binding))
    assert counts(database) == before


@pytest.mark.parametrize(
    "field",
    [
        "scope",
        "plan_digest",
        "binding_digest",
        "evidence_ref",
        "signal_id",
        "instance_id",
        "intent_id",
        "opportunity_id",
    ],
)
def test_claim_helper_rejects_every_changed_identity_field(database, field):
    _, binding, plan, episode, expected = generic_intent_without_claim(database)
    payload = {
        **expected,
        field: {"account_id": "other"} if field == "scope" else "wrong",
    }
    BusinessState(database).change(
        opportunity_key(plan),
        expected_version=0,
        request_key="admit:" + episode,
        payload=payload,
        reason="strategy plan admitted",
    )
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(conn, episode, deployment=deployment_snapshot(binding))


@pytest.mark.parametrize(
    "changes",
    [{"reason": "not-admission"}, {"request_key": "not-the-original-request"}],
)
def test_claim_helper_verifies_immutable_history_not_only_current_payload(
    database, changes
):
    _, binding, plan, episode, expected = generic_intent_without_claim(database)
    BusinessState(database).change(
        opportunity_key(plan),
        expected_version=0,
        payload=expected,
        **{
            "request_key": "admit:" + episode,
            "reason": "strategy plan admitted",
            **changes,
        },
    )
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(conn, episode, deployment=deployment_snapshot(binding))


def test_claim_helper_requires_atomic_signal_receipt(database):
    _, binding, plan, episode, expected = generic_intent_without_claim(
        database, with_signal=True
    )
    BusinessState(database).change(
        opportunity_key(plan),
        expected_version=0,
        request_key="admit:" + episode,
        payload=expected,
        reason="strategy plan admitted",
    )
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(conn, episode, deployment=deployment_snapshot(binding))


@pytest.mark.parametrize("deleted", [False, True])
def test_claim_helper_rejects_rewritten_and_deleted_receipts(database, deleted):
    adapter, _, binding, plan = setup(database)
    result = adapter.admit(plan, binding, rationale="QA")
    store = BusinessState(database)
    key = opportunity_key(plan)
    import json

    payload = {} if deleted else json.loads(store.read(key).payload_json)
    store.change(
        key,
        expected_version=1,
        request_key="qa-rewrite",
        payload=payload,
        reason="QA",
        deleted=deleted,
    )
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(
            conn, result["intent_id"], deployment=deployment_snapshot(binding)
        )


def test_claim_helper_rejects_different_deployment_proof(database):
    adapter, _, binding, plan = setup(database)
    result = adapter.admit(plan, binding, rationale="QA")
    with (
        pytest.raises(
            AccountRiskDenied, match="^STRATEGY_OPPORTUNITY_RECEIPT_INVALID$"
        ),
        database() as conn,
    ):
        require_plan_admission(
            conn,
            result["intent_id"],
            deployment=deployment_snapshot(replace(binding, generation=2)),
        )
