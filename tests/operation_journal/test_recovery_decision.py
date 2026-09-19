from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    ExchangeAccess,
    OperationRecord,
    OperationStage,
    OperationType,
    RecoveryDirective,
    decide_recovery,
)
from position_identity.slot import ExchangePositionKey


def _record():
    return OperationRecord.new(
        operation_id=str(uuid4()), operation_type=OperationType.OPEN,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="recovery-decision", environment="SANDBOX",
            symbol="BTCUSDT",
        ),
        normalized_input={"quantity": 1}, now=1,
    )


def _at(stage, operation_type=OperationType.OPEN):
    changes = {"stage": stage, "operation_type": operation_type, "version": 2}
    if stage in {OperationStage.FAILED_RETRYABLE, OperationStage.AUXILIARY_PENDING}:
        changes.update(
            pending_requirements_json='["retry_named_write"]',
            next_attempt_at=10,
        )
    return replace(_record(), **changes)


def test_every_operation_stage_has_an_explicit_decision():
    decisions = {stage: decide_recovery(_at(stage)) for stage in OperationStage}
    assert set(decisions) == set(OperationStage)
    assert all(isinstance(value.directive, RecoveryDirective)
               for value in decisions.values())


@pytest.mark.parametrize("stage", [OperationStage.SUBMITTING, OperationStage.UNKNOWN])
def test_ambiguous_submission_is_query_only_and_requires_absence_proof(stage):
    decision = decide_recovery(_at(stage))
    assert decision.directive is RecoveryDirective.RECONCILE_EXCHANGE_MUTATION
    assert decision.exchange_access is ExchangeAccess.QUERY_ONLY
    assert decision.effect_absence_proof_required is True
    assert decision.automatic is True


def test_intent_preflight_is_the_only_conditional_exchange_mutation_grant():
    decisions = [decide_recovery(_at(stage)) for stage in OperationStage]
    mutation_grants = [
        decision for decision in decisions
        if decision.exchange_access is ExchangeAccess.CONDITIONAL_MUTATION
    ]
    assert len(mutation_grants) == 1
    assert mutation_grants[0].directive is RecoveryDirective.PREFLIGHT_SUBMISSION


def test_ghost_and_external_intents_never_gain_submission_permission():
    ghost = decide_recovery(_at(
        OperationStage.INTENT_DURABLE, OperationType.GHOST_FINALIZE))
    assert ghost.directive is RecoveryDirective.VERIFY_GHOST_FLAT
    assert ghost.exchange_access is ExchangeAccess.QUERY_ONLY

    external = decide_recovery(_at(
        OperationStage.INTENT_DURABLE, OperationType.EXTERNAL_RECONCILE))
    assert external.directive is RecoveryDirective.CLASSIFY_EXTERNAL_EXPOSURE
    assert external.exchange_access is ExchangeAccess.QUERY_ONLY
    assert external.policy_decision_required is True


def test_local_and_auxiliary_replay_cannot_touch_exchange():
    expected = {
        OperationStage.EFFECT_CONFIRMED: RecoveryDirective.REPLAY_LOCAL_PROJECTION,
        OperationStage.LOCAL_PROJECTED: RecoveryDirective.REPLAY_FINALIZATION,
        OperationStage.AUXILIARY_PENDING: RecoveryDirective.REPLAY_DURABLE_AUXILIARY,
        OperationStage.FAILED_RETRYABLE: RecoveryDirective.RETRY_PROVEN_SAFE_ACTION,
    }
    for stage, directive in expected.items():
        decision = decide_recovery(_at(stage))
        assert decision.directive is directive
        assert decision.exchange_access is ExchangeAccess.FORBIDDEN
    assert decide_recovery(
        _at(OperationStage.AUXILIARY_PENDING)
    ).pending_requirements == ("retry_named_write",)


def test_terminal_and_compensation_states_do_not_auto_execute():
    completed = decide_recovery(_at(OperationStage.COMPLETED))
    assert completed.directive is RecoveryDirective.NONE
    assert completed.automatic is False

    failed = decide_recovery(_at(OperationStage.FAILED_TERMINAL))
    assert failed.directive is RecoveryDirective.OPERATOR_REVIEW
    assert failed.automatic is False

    compensation = decide_recovery(_at(OperationStage.COMPENSATION_REQUIRED))
    assert compensation.directive is RecoveryDirective.CREATE_COMPENSATION_OPERATION
    assert compensation.policy_decision_required is True
    assert compensation.automatic is False


def test_recovery_decision_rejects_untyped_input():
    with pytest.raises(TypeError, match="OperationRecord"):
        decide_recovery({})
