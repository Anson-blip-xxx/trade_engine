"""Fail-closed composition of claimed operations and recovery decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from operation_journal.model import OperationRecord, OperationStage
from operation_journal.postgres import RecoveryClaimCode, RecoveryClaimResult
from operation_journal.recovery import RecoveryDecision, decide_recovery


class RecoveryBatchDisposition(str, Enum):
    READY = "READY"
    IDLE = "IDLE"
    JOURNAL_UNKNOWN = "JOURNAL_UNKNOWN"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


@dataclass(frozen=True)
class RecoveryWorkItem:
    record: OperationRecord
    decision: RecoveryDecision


@dataclass(frozen=True)
class RecoveryBatchPlan:
    disposition: RecoveryBatchDisposition
    automatic_items: tuple[RecoveryWorkItem, ...] = ()
    gated_items: tuple[RecoveryWorkItem, ...] = ()
    reason: str | None = None

    @property
    def items(self) -> tuple[RecoveryWorkItem, ...]:
        return self.automatic_items + self.gated_items


_TERMINAL = frozenset({OperationStage.COMPLETED, OperationStage.FAILED_TERMINAL})


def plan_recovery_batch(claim: RecoveryClaimResult) -> RecoveryBatchPlan:
    """Validate one journal claim result and classify work without executing it."""
    if not isinstance(claim, RecoveryClaimResult):
        raise TypeError("claim must be RecoveryClaimResult")
    if claim.code is RecoveryClaimCode.EMPTY:
        if claim.records:
            return _invalid("EMPTY claim unexpectedly contained records")
        return RecoveryBatchPlan(RecoveryBatchDisposition.IDLE)
    if claim.code is RecoveryClaimCode.UNKNOWN:
        if claim.records:
            return _invalid("UNKNOWN claim cannot expose unacknowledged records")
        return RecoveryBatchPlan(
            RecoveryBatchDisposition.JOURNAL_UNKNOWN,
            reason="reconcile journal claim before any recovery action",
        )
    if claim.code is not RecoveryClaimCode.CLAIMED:
        return _invalid("unsupported recovery claim code")
    if not claim.records:
        return _invalid("CLAIMED result contained no records")

    operation_ids = [record.operation_id for record in claim.records]
    slot_digests = [
        record.exchange_position_key.canonical_digest()
        for record in claim.records
    ]
    owners = {record.owner_token for record in claim.records}
    if len(set(operation_ids)) != len(operation_ids):
        return _invalid("claimed batch contains duplicate operation_id")
    if len(set(slot_digests)) != len(slot_digests):
        return _invalid("claimed batch contains more than one operation per slot")
    if None in owners or len(owners) != 1:
        return _invalid("claimed batch must have exactly one nonempty owner")
    if any(record.lease_expires_at is None for record in claim.records):
        return _invalid("claimed record is missing lease expiry")
    if any(record.stage in _TERMINAL for record in claim.records):
        return _invalid("claimed batch contains terminal operation")

    automatic = []
    gated = []
    for record in claim.records:
        item = RecoveryWorkItem(record, decide_recovery(record))
        (automatic if item.decision.automatic else gated).append(item)
    return RecoveryBatchPlan(
        RecoveryBatchDisposition.READY,
        automatic_items=tuple(automatic),
        gated_items=tuple(gated),
    )


def _invalid(reason):
    return RecoveryBatchPlan(
        RecoveryBatchDisposition.INVARIANT_VIOLATION,
        reason=reason,
    )

