"""Pure D3D fence between recovery work and canonical slot/protection state."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from operation_journal.model import OperationRecord, OperationType
from position_identity.authority import AuthorityStatus, SlotAuthority
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus


class GenerationFenceCode(str, Enum):
    CURRENT = "CURRENT"
    ALLOCATION_REQUIRED = "ALLOCATION_REQUIRED"
    POLICY_REQUIRED = "POLICY_REQUIRED"
    MISSING_BINDING = "MISSING_BINDING"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INACTIVE_SLOT = "INACTIVE_SLOT"
    QUARANTINED = "QUARANTINED"
    DESIRED_REQUIRED = "DESIRED_REQUIRED"
    STALE_PROTECTION = "STALE_PROTECTION"


@dataclass(frozen=True)
class GenerationFenceDecision:
    code: GenerationFenceCode
    fence_passed: bool
    reason: str


_CLOSE_TYPES = frozenset({
    OperationType.CLOSE_FULL,
    OperationType.CLOSE_PARTIAL,
    OperationType.GHOST_FINALIZE,
})
_PROTECTION_TYPES = frozenset({
    OperationType.PROTECTION_CREATE,
    OperationType.PROTECTION_REPLACE,
})
_LIVE_PROTECTION_STATUSES = frozenset({
    ProtectionStatus.PENDING,
    ProtectionStatus.SUBMITTING,
    ProtectionStatus.UNKNOWN,
    ProtectionStatus.ACTIVE,
    ProtectionStatus.REPLACING,
})


def validate_recovery_generation(
        record: OperationRecord,
        authority: SlotAuthority,
        desired: DesiredProtectionRecord | None = None,
        ) -> GenerationFenceDecision:
    """Decide whether claimed recovery work still names current authority."""
    if not isinstance(record, OperationRecord):
        raise TypeError("record must be OperationRecord")
    if not isinstance(authority, SlotAuthority):
        raise TypeError("authority must be SlotAuthority")
    if desired is not None and not isinstance(desired, DesiredProtectionRecord):
        raise TypeError("desired must be DesiredProtectionRecord or None")
    if record.exchange_position_key != authority.exchange_position_key:
        return _blocked(
            GenerationFenceCode.IDENTITY_MISMATCH,
            "operation and slot authority keys differ",
        )
    if authority.status is AuthorityStatus.QUARANTINED:
        return _blocked(
            GenerationFenceCode.QUARANTINED,
            "quarantined slot requires operator policy",
        )
    if record.operation_type is OperationType.OPEN:
        if authority.status is AuthorityStatus.FLAT:
            if (record.position_episode_id is None and
                    record.lifecycle_generation is None):
                return _blocked(
                    GenerationFenceCode.ALLOCATION_REQUIRED,
                    "fresh open must atomically allocate the next episode",
                )
            return _blocked(
                GenerationFenceCode.IDENTITY_MISMATCH,
                "open binding does not name a current active episode",
            )
        return _blocked(
            GenerationFenceCode.POLICY_REQUIRED,
            "active-slot open requires duplicate/scale-in product policy",
        )
    if record.operation_type is OperationType.EXTERNAL_RECONCILE:
        return _blocked(
            GenerationFenceCode.POLICY_REQUIRED,
            "external exposure disposition cannot be inferred by recovery",
        )

    missing = []
    if record.position_episode_id is None:
        missing.append("position_episode_id")
    if record.lifecycle_generation is None:
        missing.append("lifecycle_generation")
    if missing:
        return _blocked(
            GenerationFenceCode.MISSING_BINDING,
            f"operation is missing {','.join(missing)}",
        )
    if (authority.episode_id != record.position_episode_id or
            authority.slot_generation != record.lifecycle_generation):
        return _blocked(
            GenerationFenceCode.IDENTITY_MISMATCH,
            "operation episode/generation is stale for the current slot",
        )
    if record.operation_type in _CLOSE_TYPES:
        return GenerationFenceDecision(
            GenerationFenceCode.CURRENT,
            fence_passed=True,
            reason="operation matches the retained current episode generation",
        )
    if record.operation_type not in _PROTECTION_TYPES:
        raise AssertionError(f"unhandled operation type: {record.operation_type!r}")
    if authority.status is not AuthorityStatus.ACTIVE:
        return _blocked(
            GenerationFenceCode.INACTIVE_SLOT,
            "protection work requires an active slot",
        )
    if record.protection_generation is None:
        return _blocked(
            GenerationFenceCode.MISSING_BINDING,
            "protection operation is missing protection_generation",
        )
    if desired is None:
        return _blocked(
            GenerationFenceCode.DESIRED_REQUIRED,
            "protection work requires current desired state",
        )
    if (desired.exchange_position_key != record.exchange_position_key or
            desired.episode_id != record.position_episode_id or
            desired.slot_generation != record.lifecycle_generation or
            desired.protection_generation != record.protection_generation or
            desired.last_operation_id != record.operation_id):
        return _blocked(
            GenerationFenceCode.IDENTITY_MISMATCH,
            "desired protection does not match operation identity/generation",
        )
    if desired.status not in _LIVE_PROTECTION_STATUSES:
        return _blocked(
            GenerationFenceCode.STALE_PROTECTION,
            "desired protection is terminal or superseded",
        )
    return GenerationFenceDecision(
        GenerationFenceCode.CURRENT,
        fence_passed=True,
        reason="operation matches current slot and desired protection generations",
    )


def _blocked(code, reason):
    return GenerationFenceDecision(code, fence_passed=False, reason=reason)
