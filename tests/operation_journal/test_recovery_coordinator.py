from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    OperationRecord,
    OperationStage,
    OperationType,
    RecoveryBatchDisposition,
    RecoveryClaimCode,
    RecoveryClaimResult,
    RecoveryDirective,
    plan_recovery_batch,
)
from position_identity.slot import ExchangePositionKey


def _claimed(*, symbol="BTCUSDT", stage=OperationStage.UNKNOWN,
             owner="recovery-owner"):
    return OperationRecord(
        operation_id=str(uuid4()), operation_type=OperationType.OPEN,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="recovery-coordinator", environment="SANDBOX",
            symbol=symbol,
        ),
        stage=stage, version=3, input_json="{}", created_at=1, updated_at=2,
        owner_token=owner, lease_expires_at=30,
    )


def test_empty_and_unknown_claims_are_distinct_and_execute_nothing():
    idle = plan_recovery_batch(RecoveryClaimResult(RecoveryClaimCode.EMPTY))
    assert idle.disposition is RecoveryBatchDisposition.IDLE
    assert idle.items == ()

    unknown = plan_recovery_batch(RecoveryClaimResult(RecoveryClaimCode.UNKNOWN))
    assert unknown.disposition is RecoveryBatchDisposition.JOURNAL_UNKNOWN
    assert unknown.items == ()
    assert "before any recovery action" in unknown.reason


def test_claimed_records_are_decided_and_partitioned_without_execution():
    automatic = _claimed()
    gated = _claimed(symbol="ETHUSDT", stage=OperationStage.COMPENSATION_REQUIRED)
    plan = plan_recovery_batch(RecoveryClaimResult(
        RecoveryClaimCode.CLAIMED, (automatic, gated)))
    assert plan.disposition is RecoveryBatchDisposition.READY
    assert [item.record for item in plan.automatic_items] == [automatic]
    assert plan.automatic_items[0].decision.directive is (
        RecoveryDirective.RECONCILE_EXCHANGE_MUTATION)
    assert [item.record for item in plan.gated_items] == [gated]
    assert plan.gated_items[0].decision.directive is (
        RecoveryDirective.CREATE_COMPENSATION_OPERATION)


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        ((), "contained no records"),
        ((_claimed(owner="owner-a"),
          _claimed(symbol="ETHUSDT", owner="owner-b")), "exactly one nonempty owner"),
        ((_claimed(stage=OperationStage.COMPLETED),), "terminal operation"),
    ],
)
def test_malformed_claimed_batches_fail_closed(records, reason):
    plan = plan_recovery_batch(RecoveryClaimResult(
        RecoveryClaimCode.CLAIMED, records))
    assert plan.disposition is RecoveryBatchDisposition.INVARIANT_VIOLATION
    assert plan.items == ()
    assert reason in plan.reason


def test_duplicate_operation_or_slot_fails_closed():
    first = _claimed()
    duplicate_id = replace(
        _claimed(symbol="ETHUSDT"), operation_id=first.operation_id)
    duplicate_slot = _claimed()
    for records, reason in (
        ((first, duplicate_id), "duplicate operation_id"),
        ((first, duplicate_slot), "one operation per slot"),
    ):
        plan = plan_recovery_batch(RecoveryClaimResult(
            RecoveryClaimCode.CLAIMED, records))
        assert plan.disposition is RecoveryBatchDisposition.INVARIANT_VIOLATION
        assert reason in plan.reason


def test_coordinator_rejects_untyped_input():
    with pytest.raises(TypeError, match="RecoveryClaimResult"):
        plan_recovery_batch(None)
