"""Pure recovery classification for durable operation stages."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from operation_journal.model import OperationRecord, OperationStage, OperationType


class RecoveryDirective(str, Enum):
    VALIDATE_INTENT = "VALIDATE_INTENT"
    PREFLIGHT_SUBMISSION = "PREFLIGHT_SUBMISSION"
    VERIFY_GHOST_FLAT = "VERIFY_GHOST_FLAT"
    CLASSIFY_EXTERNAL_EXPOSURE = "CLASSIFY_EXTERNAL_EXPOSURE"
    RECONCILE_EXCHANGE_MUTATION = "RECONCILE_EXCHANGE_MUTATION"
    QUERY_EXCHANGE_EFFECT = "QUERY_EXCHANGE_EFFECT"
    REPLAY_LOCAL_PROJECTION = "REPLAY_LOCAL_PROJECTION"
    REPLAY_FINALIZATION = "REPLAY_FINALIZATION"
    REPLAY_DURABLE_AUXILIARY = "REPLAY_DURABLE_AUXILIARY"
    RETRY_PROVEN_SAFE_ACTION = "RETRY_PROVEN_SAFE_ACTION"
    CREATE_COMPENSATION_OPERATION = "CREATE_COMPENSATION_OPERATION"
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    NONE = "NONE"


class ExchangeAccess(str, Enum):
    FORBIDDEN = "FORBIDDEN"
    QUERY_ONLY = "QUERY_ONLY"
    CONDITIONAL_MUTATION = "CONDITIONAL_MUTATION"


@dataclass(frozen=True)
class RecoveryDecision:
    directive: RecoveryDirective
    exchange_access: ExchangeAccess
    generation_fence_required: bool
    effect_absence_proof_required: bool
    policy_decision_required: bool
    automatic: bool
    pending_requirements: tuple[str, ...] = ()


_MUTATING_TYPES = frozenset({
    OperationType.OPEN,
    OperationType.CLOSE_FULL,
    OperationType.CLOSE_PARTIAL,
    OperationType.PROTECTION_CREATE,
    OperationType.PROTECTION_REPLACE,
})


def _decision(directive, exchange_access=ExchangeAccess.FORBIDDEN, *,
              fence=True, absence=False, policy=False, automatic=True,
              pending=()):
    return RecoveryDecision(
        directive=directive,
        exchange_access=exchange_access,
        generation_fence_required=fence,
        effect_absence_proof_required=absence,
        policy_decision_required=policy,
        automatic=automatic,
        pending_requirements=tuple(pending),
    )


def decide_recovery(record: OperationRecord) -> RecoveryDecision:
    """Classify the next safe action without executing or scheduling it."""
    if not isinstance(record, OperationRecord):
        raise TypeError("record must be OperationRecord")

    stage = record.stage
    if stage is OperationStage.NEW:
        return _decision(RecoveryDirective.VALIDATE_INTENT, fence=False)
    if stage is OperationStage.INTENT_DURABLE:
        if record.operation_type in _MUTATING_TYPES:
            return _decision(
                RecoveryDirective.PREFLIGHT_SUBMISSION,
                ExchangeAccess.CONDITIONAL_MUTATION,
            )
        if record.operation_type is OperationType.GHOST_FINALIZE:
            return _decision(
                RecoveryDirective.VERIFY_GHOST_FLAT,
                ExchangeAccess.QUERY_ONLY,
            )
        return _decision(
            RecoveryDirective.CLASSIFY_EXTERNAL_EXPOSURE,
            ExchangeAccess.QUERY_ONLY,
            policy=True,
        )
    if stage in {OperationStage.SUBMITTING, OperationStage.UNKNOWN}:
        if record.operation_type is OperationType.GHOST_FINALIZE:
            directive = RecoveryDirective.VERIFY_GHOST_FLAT
        elif record.operation_type is OperationType.EXTERNAL_RECONCILE:
            directive = RecoveryDirective.CLASSIFY_EXTERNAL_EXPOSURE
        else:
            directive = RecoveryDirective.RECONCILE_EXCHANGE_MUTATION
        return _decision(
            directive,
            ExchangeAccess.QUERY_ONLY,
            absence=True,
            policy=record.operation_type is OperationType.EXTERNAL_RECONCILE,
        )
    if stage is OperationStage.EXCHANGE_ACKED:
        return _decision(
            RecoveryDirective.QUERY_EXCHANGE_EFFECT,
            ExchangeAccess.QUERY_ONLY,
        )
    if stage is OperationStage.EFFECT_CONFIRMED:
        return _decision(RecoveryDirective.REPLAY_LOCAL_PROJECTION)
    if stage is OperationStage.LOCAL_PROJECTED:
        return _decision(RecoveryDirective.REPLAY_FINALIZATION)
    if stage is OperationStage.AUXILIARY_PENDING:
        return _decision(
            RecoveryDirective.REPLAY_DURABLE_AUXILIARY,
            pending=record.pending_requirements,
        )
    if stage is OperationStage.FAILED_RETRYABLE:
        return _decision(
            RecoveryDirective.RETRY_PROVEN_SAFE_ACTION,
            pending=record.pending_requirements,
        )
    if stage is OperationStage.COMPENSATION_REQUIRED:
        return _decision(
            RecoveryDirective.CREATE_COMPENSATION_OPERATION,
            policy=True,
            automatic=False,
        )
    if stage is OperationStage.FAILED_TERMINAL:
        return _decision(
            RecoveryDirective.OPERATOR_REVIEW,
            fence=False,
            policy=True,
            automatic=False,
        )
    if stage is OperationStage.COMPLETED:
        return _decision(
            RecoveryDirective.NONE,
            fence=False,
            automatic=False,
        )
    raise AssertionError(f"unhandled operation stage: {stage!r}")

