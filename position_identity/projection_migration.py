"""Dormant legacy-to-canonical projection compatibility and writer guard."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from position_identity.authority import (
    AuthorityProvenance,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
)


class LegacyProjectionCode(str, Enum):
    READY = 'READY'
    ALREADY_PROJECTED = 'ALREADY_PROJECTED'
    QUARANTINE = 'QUARANTINE'
    INVALID = 'INVALID'


class LegacyProjectionReason(str, Enum):
    AUTHORITY_NOT_ADOPTED = 'AUTHORITY_NOT_ADOPTED'
    AUTHORITY_MISMATCH = 'AUTHORITY_MISMATCH'
    MALFORMED_LEGACY_ROW = 'MALFORMED_LEGACY_ROW'
    MIXED_VERSION = 'MIXED_VERSION'
    PROJECTION_UNAVAILABLE = 'PROJECTION_UNAVAILABLE'
    PROJECTION_MALFORMED = 'PROJECTION_MALFORMED'


class LegacyWriterDecision(str, Enum):
    ALLOW_WHILE_UNMIGRATED = 'ALLOW_WHILE_UNMIGRATED'
    BLOCK_CANONICAL_PRESENT = 'BLOCK_CANONICAL_PRESENT'
    FAIL_CLOSED = 'FAIL_CLOSED'


@dataclass(frozen=True)
class LegacyProjectionResult:
    code: LegacyProjectionCode
    projection: LivePositionProjection | None = None
    reason: LegacyProjectionReason | None = None
    message: str = ''


_CANONICAL_FIELDS = frozenset({
    'schema_version', 'episode_id', 'slot_generation',
    'identity_provenance', 'state_revision', 'last_operation_id',
    'exchange_position_key',
})


def legacy_snapshot_write_decision(
        canonical_read: ProjectionReadResult) -> LegacyWriterDecision:
    """Fail closed once a slot has canonical state or cannot be classified."""
    if not isinstance(canonical_read, ProjectionReadResult):
        return LegacyWriterDecision.FAIL_CLOSED
    if canonical_read.code is ProjectionReadCode.NOT_FOUND \
            and canonical_read.projection is None:
        return LegacyWriterDecision.ALLOW_WHILE_UNMIGRATED
    if canonical_read.code is ProjectionReadCode.FOUND \
            and isinstance(
                canonical_read.projection, LivePositionProjection):
        return LegacyWriterDecision.BLOCK_CANONICAL_PRESENT
    return LegacyWriterDecision.FAIL_CLOSED


def prepare_legacy_projection(
        *, legacy_row, authority: SlotAuthority,
        canonical_read: ProjectionReadResult,
        operation_id: str, now: float) -> LegacyProjectionResult:
    """Prepare a projection only after controlled MIGRATED adoption succeeded."""
    if not isinstance(authority, SlotAuthority) \
            or authority.status is not AuthorityStatus.ACTIVE \
            or authority.provenance is not AuthorityProvenance.MIGRATED:
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=LegacyProjectionReason.AUTHORITY_NOT_ADOPTED)
    if not isinstance(canonical_read, ProjectionReadResult):
        return LegacyProjectionResult(
            LegacyProjectionCode.INVALID,
            reason=LegacyProjectionReason.PROJECTION_MALFORMED)
    if canonical_read.code is ProjectionReadCode.UNAVAILABLE:
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=LegacyProjectionReason.PROJECTION_UNAVAILABLE)
    if canonical_read.code is ProjectionReadCode.MALFORMED:
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=LegacyProjectionReason.PROJECTION_MALFORMED)
    if canonical_read.code is ProjectionReadCode.FOUND:
        existing = canonical_read.projection
        if isinstance(existing, LivePositionProjection) \
                and existing.exchange_position_key \
                == authority.exchange_position_key \
                and existing.episode_id == authority.episode_id \
                and existing.slot_generation == authority.slot_generation \
                and existing.identity_provenance \
                is AuthorityProvenance.MIGRATED \
                and existing.legacy_position_id_alias \
                == authority.legacy_position_id_alias:
            return LegacyProjectionResult(
                LegacyProjectionCode.ALREADY_PROJECTED, projection=existing)
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=LegacyProjectionReason.MIXED_VERSION)
    if canonical_read.code is not ProjectionReadCode.NOT_FOUND:
        return LegacyProjectionResult(
            LegacyProjectionCode.INVALID,
            reason=LegacyProjectionReason.PROJECTION_MALFORMED)
    if not isinstance(legacy_row, dict) \
            or _CANONICAL_FIELDS.intersection(legacy_row):
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=(LegacyProjectionReason.MIXED_VERSION
                    if isinstance(legacy_row, dict)
                    else LegacyProjectionReason.MALFORMED_LEGACY_ROW))

    try:
        symbol = legacy_row['symbol'].strip().upper()
        alias = legacy_row['position_id'].strip()
        side = legacy_row['side']
        system = legacy_row['system']
        quantity = legacy_row['qty']
        entry_price = legacy_row['entry']
        opened_at = legacy_row['open_time']
        if symbol != authority.exchange_position_key.symbol \
                or alias != authority.legacy_position_id_alias:
            raise ValueError('legacy identity does not match adopted authority')
        projection = LivePositionProjection(
            exchange_position_key=authority.exchange_position_key,
            episode_id=authority.episode_id,
            slot_generation=authority.slot_generation,
            identity_provenance=authority.provenance,
            state_revision=1,
            last_operation_id=operation_id,
            side=side,
            system=system,
            quantity=quantity,
            entry_price=entry_price,
            opened_at=opened_at,
            updated_at=now,
            legacy_position_id_alias=alias,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        reason = (LegacyProjectionReason.AUTHORITY_MISMATCH
                  if 'adopted authority' in str(exc)
                  else LegacyProjectionReason.MALFORMED_LEGACY_ROW)
        return LegacyProjectionResult(
            LegacyProjectionCode.QUARANTINE,
            reason=reason, message=str(exc))
    return LegacyProjectionResult(
        LegacyProjectionCode.READY, projection=projection)
