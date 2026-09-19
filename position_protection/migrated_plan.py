"""Pure desired-protection planning after an acknowledged legacy handoff."""
from dataclasses import dataclass
from enum import Enum

from position_identity.authority import AuthorityProvenance, AuthorityStatus
from position_identity.migration_handoff import (
    LegacyMigrationHandoffCode,
    LegacyMigrationHandoffResult,
)
from position_protection.desired import DesiredProtectionRecord


class MigratedProtectionPlanCode(str, Enum):
    READY = "READY"
    RESOLVE_HANDOFF_UNKNOWN = "RESOLVE_HANDOFF_UNKNOWN"
    RETRY_HANDOFF = "RETRY_HANDOFF"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class MigratedProtectionPlan:
    code: MigratedProtectionPlanCode
    desired: DesiredProtectionRecord | None = None
    message: str = ""


def plan_migrated_protection(
    *, handoff: LegacyMigrationHandoffResult, legacy_row: dict,
    desired_intent_id: str, operation_id: str, now: float,
) -> MigratedProtectionPlan:
    if not isinstance(handoff, LegacyMigrationHandoffResult):
        raise TypeError("handoff must be LegacyMigrationHandoffResult")
    if handoff.code is LegacyMigrationHandoffCode.UNKNOWN:
        return MigratedProtectionPlan(MigratedProtectionPlanCode.RESOLVE_HANDOFF_UNKNOWN)
    if handoff.code is LegacyMigrationHandoffCode.UNAVAILABLE:
        return MigratedProtectionPlan(MigratedProtectionPlanCode.RETRY_HANDOFF)
    if handoff.code in (
        LegacyMigrationHandoffCode.QUARANTINED,
        LegacyMigrationHandoffCode.EXISTING_OWNER,
        LegacyMigrationHandoffCode.CONFLICT,
        LegacyMigrationHandoffCode.MALFORMED,
    ):
        return MigratedProtectionPlan(MigratedProtectionPlanCode.QUARANTINE)
    if not handoff.applied:
        return MigratedProtectionPlan(MigratedProtectionPlanCode.REJECT)
    authority, projection = handoff.authority, handoff.projection
    if (
        authority is None or projection is None
        or authority.status is not AuthorityStatus.ACTIVE
        or authority.provenance is not AuthorityProvenance.MIGRATED
        or authority.exchange_position_key != projection.exchange_position_key
        or authority.episode_id != projection.episode_id
        or authority.slot_generation != projection.slot_generation
    ):
        return MigratedProtectionPlan(
            MigratedProtectionPlanCode.QUARANTINE,
            message="acknowledged handoff identity is incomplete or inconsistent",
        )
    if not isinstance(legacy_row, dict):
        return MigratedProtectionPlan(MigratedProtectionPlanCode.REJECT)
    alias = legacy_row.get("algo_sl_id")
    if isinstance(alias, bool) or not isinstance(alias, (str, int)) or not str(alias).strip():
        return MigratedProtectionPlan(
            MigratedProtectionPlanCode.QUARANTINE,
            message="legacy protection alias is required",
        )
    try:
        pending = DesiredProtectionRecord.initial_pending(
            exchange_position_key=authority.exchange_position_key,
            episode_id=authority.episode_id,
            slot_generation=authority.slot_generation,
            desired_intent_id=desired_intent_id,
            trigger_price=legacy_row.get("sl"),
            covered_quantity=projection.quantity,
            closing_side="SELL" if projection.side == "LONG" else "BUY",
            operation_id=operation_id,
            now=now,
        )
        desired = pending.transition(
            status="PENDING", operation_id=operation_id, now=now,
            exchange_algo_aliases=(str(alias).strip(),),
            allow_same_status_alias_repair=True,
        )
    except (TypeError, ValueError) as exc:
        return MigratedProtectionPlan(
            MigratedProtectionPlanCode.QUARANTINE, message=str(exc)
        )
    return MigratedProtectionPlan(MigratedProtectionPlanCode.READY, desired)
