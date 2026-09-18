"""Dormant single-slot episode-authority onboarding primitives."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol
from uuid import UUID

from position_identity.authority import (
    AuthorityAck,
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey, PositionMode, SlotSide


class AdoptionClassification(str, Enum):
    NATIVE_AUTHORIZED = 'NATIVE_AUTHORIZED'
    LEGACY_ADOPTABLE = 'LEGACY_ADOPTABLE'
    LEGACY_QUARANTINE = 'LEGACY_QUARANTINE'
    RECONSTRUCTED_QUARANTINE = 'RECONSTRUCTED_QUARANTINE'
    ALREADY_ADOPTED = 'ALREADY_ADOPTED'
    ALREADY_RECONSTRUCTED = 'ALREADY_RECONSTRUCTED'
    CONFLICT = 'CONFLICT'
    INVALID = 'INVALID'


class QuarantineReason(str, Enum):
    ACCOUNT_UNKNOWN = 'ACCOUNT_UNKNOWN'
    SLOT_AMBIGUOUS = 'SLOT_AMBIGUOUS'
    EXCHANGE_LOCAL_MISMATCH = 'EXCHANGE_LOCAL_MISMATCH'
    AUTHORITY_CONFLICT = 'AUTHORITY_CONFLICT'
    AUTHORITY_MISSING = 'AUTHORITY_MISSING'
    MALFORMED_AUTHORITY = 'MALFORMED_AUTHORITY'
    MALFORMED_LOCAL_STATE = 'MALFORMED_LOCAL_STATE'
    MISSING_LEGACY_ID = 'MISSING_LEGACY_ID'
    MIXED_VERSION = 'MIXED_VERSION'
    BACKEND_UNAVAILABLE = 'BACKEND_UNAVAILABLE'


class OnboardingResultCode(str, Enum):
    ADOPTED = 'ADOPTED'
    ALREADY_ADOPTED = 'ALREADY_ADOPTED'
    EXISTING_OWNER = 'EXISTING_OWNER'
    RECONSTRUCTED_QUARANTINED = 'RECONSTRUCTED_QUARANTINED'
    ALREADY_RECONSTRUCTED = 'ALREADY_RECONSTRUCTED'
    CONFLICT = 'CONFLICT'
    QUARANTINED = 'QUARANTINED'
    BACKEND_ERROR = 'BACKEND_ERROR'
    INVALID = 'INVALID'


@dataclass(frozen=True)
class ClassificationDecision:
    classification: AdoptionClassification
    reason: QuarantineReason | None = None
    authority: SlotAuthority | None = None


@dataclass(frozen=True)
class AuthorityExpectation:
    revision: int
    slot_generation: int
    episode_id: str | None
    status: AuthorityStatus = AuthorityStatus.FLAT


@dataclass(frozen=True)
class AdoptionPlan:
    slot_key: ExchangePositionKey | None
    candidate_episode_id: str | None
    expected_authority: AuthorityExpectation | None
    legacy_position_id_alias: str | None
    local_symbol: str | None
    local_side: str | None
    exchange_side: str | None
    mixed_version: bool | None
    provenance: AuthorityProvenance
    classification: AdoptionClassification
    reason: QuarantineReason | None
    local_quantity: Decimal | None
    exchange_quantity: Decimal | None
    quantity_tolerance: Decimal | None
    requires_authority_initialization: bool = False
    existing_authority: SlotAuthority | None = None

    @property
    def candidate_slot_generation(self) -> int | None:
        if self.classification is not AdoptionClassification.LEGACY_ADOPTABLE:
            return None
        if self.expected_authority is None:
            return 1 if self.requires_authority_initialization else None
        return self.expected_authority.slot_generation + 1


@dataclass(frozen=True)
class ReconstructionPlan:
    slot_key: ExchangePositionKey | None
    candidate_episode_id: str | None
    expected_authority: AuthorityExpectation | None
    provenance: AuthorityProvenance
    status: AuthorityStatus
    classification: AdoptionClassification
    reason: QuarantineReason | None
    exchange_side: str | None
    exchange_quantity: Decimal | None
    requires_authority_initialization: bool = False
    existing_authority: SlotAuthority | None = None

    @property
    def candidate_slot_generation(self) -> int | None:
        if self.classification is not \
                AdoptionClassification.RECONSTRUCTED_QUARANTINE:
            return None
        if self.expected_authority is None:
            return 1 if self.requires_authority_initialization else None
        return self.expected_authority.slot_generation + 1


@dataclass(frozen=True)
class OnboardingResult:
    code: OnboardingResultCode
    authority: SlotAuthority | None = None
    reason: QuarantineReason | None = None
    candidate_discarded: bool = False
    message: str = ''


class AuthorityStore(Protocol):
    def get_slot_authority(
            self, key: ExchangePositionKey) -> AuthorityReadResult: ...

    def initialize_flat(
            self, key: ExchangePositionKey, *, now: float) -> AuthorityAck: ...

    def adopt_if_unowned(
            self, key: ExchangePositionKey, *, candidate_episode_id: str,
            legacy_position_id_alias: str,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck: ...

    def create_reconstructed_quarantined(
            self, key: ExchangePositionKey, *, candidate_episode_id: str,
            legacy_position_id_alias: str | None,
            expected_revision: int, expected_slot_generation: int,
            expected_episode_id: str | None, now: float) -> AuthorityAck: ...


def _required_text(value, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{field} must be a string')
    value = value.strip()
    if not value:
        raise ValueError(f'{field} is required')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{field} contains control characters')
    return value


def _episode_id(value) -> str:
    text = _required_text(value, 'candidate_episode_id')
    try:
        return str(UUID(text))
    except (ValueError, AttributeError) as exc:
        raise ValueError('candidate_episode_id must be an opaque UUID') from exc


def _quantity(value, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f'{field} must be numeric')
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TypeError(f'{field} must be numeric') from exc
    if not quantity.is_finite() or quantity < 0 \
            or (not allow_zero and quantity == 0):
        requirement = 'nonnegative' if allow_zero else 'positive'
        raise ValueError(f'{field} must be finite and {requirement}')
    return quantity


def _side(value, field: str) -> str:
    side = _required_text(value, field).upper()
    if side not in ('LONG', 'SHORT'):
        raise ValueError(f'{field} must be LONG or SHORT')
    return side


def _expectation(authority: SlotAuthority) -> AuthorityExpectation:
    return AuthorityExpectation(
        revision=authority.revision,
        slot_generation=authority.slot_generation,
        episode_id=authority.episode_id,
        status=authority.status,
    )


def _valid_authority_read(
        authority_read, slot_key: ExchangePositionKey) -> bool:
    if not isinstance(authority_read, AuthorityReadResult) \
            or not isinstance(authority_read.code, AuthorityReadCode):
        return False
    if authority_read.code is AuthorityReadCode.FOUND:
        return (
            isinstance(authority_read.authority, SlotAuthority)
            and authority_read.authority.exchange_position_key == slot_key
        )
    return authority_read.authority is None


def _invalid_legacy_plan(
        *, slot_key, candidate_episode_id, legacy_alias,
        reason: QuarantineReason) -> AdoptionPlan:
    return AdoptionPlan(
        slot_key=slot_key if isinstance(slot_key, ExchangePositionKey) else None,
        candidate_episode_id=candidate_episode_id,
        expected_authority=None,
        legacy_position_id_alias=legacy_alias,
        local_symbol=None,
        local_side=None,
        exchange_side=None,
        mixed_version=None,
        provenance=AuthorityProvenance.MIGRATED,
        classification=AdoptionClassification.INVALID,
        reason=reason,
        local_quantity=None,
        exchange_quantity=None,
        quantity_tolerance=None,
    )


def prepare_legacy_adoption(
        *, slot_key: ExchangePositionKey | None, candidate_episode_id,
        legacy_position_id_alias, local_symbol, local_side, local_quantity,
        exchange_side, exchange_quantity, quantity_tolerance,
        authority_read: AuthorityReadResult, mixed_version: bool,
        allow_authority_initialization: bool = False) -> AdoptionPlan:
    """Build an immutable plan without writing authority or legacy state."""
    candidate = None
    alias = None
    try:
        candidate = _episode_id(candidate_episode_id)
        alias = _required_text(
            legacy_position_id_alias, 'legacy_position_id_alias')
    except (TypeError, ValueError):
        reason = (QuarantineReason.MISSING_LEGACY_ID
                  if legacy_position_id_alias is None
                  or (isinstance(legacy_position_id_alias, str)
                      and not legacy_position_id_alias.strip())
                  else QuarantineReason.MALFORMED_LOCAL_STATE)
        return _invalid_legacy_plan(
            slot_key=slot_key, candidate_episode_id=candidate,
            legacy_alias=alias, reason=reason,
        )

    if slot_key is None:
        return AdoptionPlan(
            slot_key=None,
            candidate_episode_id=candidate,
            expected_authority=None,
            legacy_position_id_alias=alias,
            local_symbol=None,
            local_side=None,
            exchange_side=None,
            mixed_version=None,
            provenance=AuthorityProvenance.MIGRATED,
            classification=AdoptionClassification.LEGACY_QUARANTINE,
            reason=QuarantineReason.ACCOUNT_UNKNOWN,
            local_quantity=None,
            exchange_quantity=None,
            quantity_tolerance=None,
        )
    if not isinstance(slot_key, ExchangePositionKey):
        return _invalid_legacy_plan(
            slot_key=slot_key, candidate_episode_id=candidate,
            legacy_alias=alias, reason=QuarantineReason.MALFORMED_LOCAL_STATE,
        )
    if slot_key.position_mode is not PositionMode.ONE_WAY \
            or slot_key.slot_side is not SlotSide.BOTH:
        return AdoptionPlan(
            slot_key=slot_key,
            candidate_episode_id=candidate,
            expected_authority=None,
            legacy_position_id_alias=alias,
            local_symbol=None,
            local_side=None,
            exchange_side=None,
            mixed_version=None,
            provenance=AuthorityProvenance.MIGRATED,
            classification=AdoptionClassification.LEGACY_QUARANTINE,
            reason=QuarantineReason.SLOT_AMBIGUOUS,
            local_quantity=None,
            exchange_quantity=None,
            quantity_tolerance=None,
        )
    try:
        normalized_symbol = _required_text(local_symbol, 'local_symbol').upper()
        normalized_local_side = _side(local_side, 'local_side')
        normalized_exchange_side = _side(exchange_side, 'exchange_side')
        normalized_local_qty = _quantity(local_quantity, 'local_quantity')
        normalized_exchange_qty = _quantity(
            exchange_quantity, 'exchange_quantity')
        normalized_tolerance = _quantity(
            quantity_tolerance, 'quantity_tolerance', allow_zero=True)
    except (TypeError, ValueError):
        return _invalid_legacy_plan(
            slot_key=slot_key, candidate_episode_id=candidate,
            legacy_alias=alias, reason=QuarantineReason.MALFORMED_LOCAL_STATE,
        )

    base = {
        'slot_key': slot_key,
        'candidate_episode_id': candidate,
        'legacy_position_id_alias': alias,
        'local_symbol': normalized_symbol,
        'local_side': normalized_local_side,
        'exchange_side': normalized_exchange_side,
        'mixed_version': mixed_version,
        'provenance': AuthorityProvenance.MIGRATED,
        'local_quantity': normalized_local_qty,
        'exchange_quantity': normalized_exchange_qty,
        'quantity_tolerance': normalized_tolerance,
    }
    if not isinstance(mixed_version, bool):
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
        )
    if mixed_version:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.LEGACY_QUARANTINE,
            reason=QuarantineReason.MIXED_VERSION,
        )
    if normalized_symbol != slot_key.symbol \
            or normalized_local_side != normalized_exchange_side \
            or abs(normalized_local_qty - normalized_exchange_qty) \
            > normalized_tolerance:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.LEGACY_QUARANTINE,
            reason=QuarantineReason.EXCHANGE_LOCAL_MISMATCH,
        )
    if not _valid_authority_read(authority_read, slot_key):
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    if authority_read.code is AuthorityReadCode.BACKEND_ERROR:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.CONFLICT,
            reason=QuarantineReason.BACKEND_UNAVAILABLE,
        )
    if authority_read.code is AuthorityReadCode.MALFORMED:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    if authority_read.code is AuthorityReadCode.NOT_FOUND:
        if not allow_authority_initialization:
            return AdoptionPlan(
                **base,
                expected_authority=None,
                classification=AdoptionClassification.LEGACY_QUARANTINE,
                reason=QuarantineReason.AUTHORITY_MISSING,
            )
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.LEGACY_ADOPTABLE,
            reason=None,
            requires_authority_initialization=True,
        )

    authority = authority_read.authority
    assert authority is not None
    if authority.status is AuthorityStatus.FLAT:
        return AdoptionPlan(
            **base,
            expected_authority=_expectation(authority),
            classification=AdoptionClassification.LEGACY_ADOPTABLE,
            reason=None,
            existing_authority=authority,
        )
    if authority.status is AuthorityStatus.ACTIVE \
            and authority.provenance is AuthorityProvenance.MIGRATED \
            and authority.legacy_position_id_alias == alias:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.ALREADY_ADOPTED,
            reason=None,
            existing_authority=authority,
        )
    if authority.status is AuthorityStatus.ACTIVE \
            and authority.provenance is AuthorityProvenance.NATIVE:
        return AdoptionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.NATIVE_AUTHORIZED,
            reason=None,
            existing_authority=authority,
        )
    classification = (AdoptionClassification.LEGACY_QUARANTINE
                      if authority.status is AuthorityStatus.QUARANTINED
                      else AdoptionClassification.CONFLICT)
    return AdoptionPlan(
        **base,
        expected_authority=None,
        classification=classification,
        reason=QuarantineReason.AUTHORITY_CONFLICT,
        existing_authority=authority,
    )


def classify_legacy_active(
        *, slot_key: ExchangePositionKey | None, legacy_position_id_alias,
        local_symbol, local_side, local_quantity, exchange_side,
        exchange_quantity, quantity_tolerance,
        authority_read: AuthorityReadResult, mixed_version: bool,
        allow_authority_initialization: bool = False) -> ClassificationDecision:
    """Classify one explicit legacy exposure without mutating any state."""
    plan = prepare_legacy_adoption(
        slot_key=slot_key,
        candidate_episode_id='00000000-0000-4000-8000-000000000000',
        legacy_position_id_alias=legacy_position_id_alias,
        local_symbol=local_symbol,
        local_side=local_side,
        local_quantity=local_quantity,
        exchange_side=exchange_side,
        exchange_quantity=exchange_quantity,
        quantity_tolerance=quantity_tolerance,
        authority_read=authority_read,
        mixed_version=mixed_version,
        allow_authority_initialization=allow_authority_initialization,
    )
    return ClassificationDecision(
        plan.classification, plan.reason, plan.existing_authority)


def prepare_reconstructed_episode(
        *, slot_key: ExchangePositionKey | None, candidate_episode_id,
        exchange_side, exchange_quantity,
        authority_read: AuthorityReadResult,
        allow_authority_initialization: bool = False) -> ReconstructionPlan:
    """Plan a quarantined reconstructed episode from exchange-only evidence."""
    try:
        candidate = _episode_id(candidate_episode_id)
    except (TypeError, ValueError):
        return ReconstructionPlan(
            slot_key=(slot_key if isinstance(slot_key, ExchangePositionKey)
                      else None),
            candidate_episode_id=None,
            expected_authority=None,
            provenance=AuthorityProvenance.RECONSTRUCTED,
            status=AuthorityStatus.QUARANTINED,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            exchange_side=None,
            exchange_quantity=None,
        )
    base = {
        'slot_key': slot_key if isinstance(slot_key, ExchangePositionKey) else None,
        'candidate_episode_id': candidate,
        'provenance': AuthorityProvenance.RECONSTRUCTED,
        'status': AuthorityStatus.QUARANTINED,
        'exchange_side': None,
    }
    if slot_key is None:
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.ACCOUNT_UNKNOWN,
            exchange_quantity=None,
        )
    if not isinstance(slot_key, ExchangePositionKey):
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            exchange_quantity=None,
        )
    if slot_key.position_mode is not PositionMode.ONE_WAY \
            or slot_key.slot_side is not SlotSide.BOTH:
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.RECONSTRUCTED_QUARANTINE,
            reason=QuarantineReason.SLOT_AMBIGUOUS,
            exchange_quantity=None,
        )
    try:
        normalized_exchange_side = _side(exchange_side, 'exchange_side')
        normalized_exchange_qty = _quantity(
            exchange_quantity, 'exchange_quantity')
    except (TypeError, ValueError):
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            exchange_quantity=None,
        )
    base['exchange_side'] = normalized_exchange_side
    base['exchange_quantity'] = normalized_exchange_qty
    if not _valid_authority_read(authority_read, slot_key):
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    if authority_read.code is AuthorityReadCode.BACKEND_ERROR:
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.CONFLICT,
            reason=QuarantineReason.BACKEND_UNAVAILABLE,
        )
    if authority_read.code is AuthorityReadCode.MALFORMED:
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    if authority_read.code is AuthorityReadCode.NOT_FOUND:
        if not allow_authority_initialization:
            return ReconstructionPlan(
                **base,
                expected_authority=None,
                classification=AdoptionClassification.CONFLICT,
                reason=QuarantineReason.AUTHORITY_MISSING,
            )
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.RECONSTRUCTED_QUARANTINE,
            reason=None,
            requires_authority_initialization=True,
        )

    authority = authority_read.authority
    assert authority is not None
    if authority.status is AuthorityStatus.FLAT:
        return ReconstructionPlan(
            **base,
            expected_authority=_expectation(authority),
            classification=AdoptionClassification.RECONSTRUCTED_QUARANTINE,
            reason=None,
            existing_authority=authority,
        )
    if authority.status is AuthorityStatus.QUARANTINED \
            and authority.provenance is AuthorityProvenance.RECONSTRUCTED:
        return ReconstructionPlan(
            **base,
            expected_authority=None,
            classification=AdoptionClassification.ALREADY_RECONSTRUCTED,
            reason=None,
            existing_authority=authority,
        )
    return ReconstructionPlan(
        **base,
        expected_authority=None,
        classification=AdoptionClassification.CONFLICT,
        reason=QuarantineReason.AUTHORITY_CONFLICT,
        existing_authority=authority,
    )


def classify_reconstructed_exposure(
        *, slot_key: ExchangePositionKey | None, exchange_side,
        exchange_quantity, authority_read: AuthorityReadResult,
        allow_authority_initialization: bool = False) -> ClassificationDecision:
    """Classify one explicit exchange-only exposure without mutating state."""
    plan = prepare_reconstructed_episode(
        slot_key=slot_key,
        candidate_episode_id='00000000-0000-4000-8000-000000000000',
        exchange_side=exchange_side,
        exchange_quantity=exchange_quantity,
        authority_read=authority_read,
        allow_authority_initialization=allow_authority_initialization,
    )
    return ClassificationDecision(
        plan.classification, plan.reason, plan.existing_authority)


def _bootstrap_flat(
        *, slot_key: ExchangePositionKey, authority_store: AuthorityStore,
        now: float) -> tuple[SlotAuthority | None, OnboardingResult | None]:
    ack = authority_store.initialize_flat(slot_key, now=now)
    if ack.code in (
            AuthorityAckCode.APPLIED, AuthorityAckCode.ALREADY_INITIALIZED):
        authority = ack.authority
        if isinstance(authority, SlotAuthority) \
                and authority.exchange_position_key == slot_key \
                and authority.status is AuthorityStatus.FLAT:
            return authority, None
    current = authority_store.get_slot_authority(slot_key)
    if current.code is AuthorityReadCode.FOUND \
            and isinstance(current.authority, SlotAuthority) \
            and current.authority.exchange_position_key == slot_key:
        return current.authority, None
    if ack.code is AuthorityAckCode.BACKEND_ERROR \
            or current.code is AuthorityReadCode.BACKEND_ERROR:
        return None, OnboardingResult(
            OnboardingResultCode.BACKEND_ERROR,
            reason=QuarantineReason.BACKEND_UNAVAILABLE,
            message=ack.message or current.message,
        )
    return None, OnboardingResult(
        OnboardingResultCode.INVALID,
        reason=QuarantineReason.MALFORMED_AUTHORITY,
        message=ack.message or current.message,
    )


def _reload(
        authority_store: AuthorityStore,
        slot_key: ExchangePositionKey) -> AuthorityReadResult:
    return authority_store.get_slot_authority(slot_key)


def _matches_expectation(
        authority: SlotAuthority,
        expectation: AuthorityExpectation) -> bool:
    return (
        authority.revision == expectation.revision
        and authority.slot_generation == expectation.slot_generation
        and authority.episode_id == expectation.episode_id
        and authority.status is expectation.status
    )


def _is_initial_flat(authority: SlotAuthority) -> bool:
    return (
        authority.status is AuthorityStatus.FLAT
        and authority.revision == 1
        and authority.slot_generation == 0
        and authority.episode_id is None
        and authority.provenance is None
    )


def _valid_expectation(expectation: AuthorityExpectation | None) -> bool:
    return (
        isinstance(expectation, AuthorityExpectation)
        and type(expectation.revision) is int
        and expectation.revision >= 1
        and type(expectation.slot_generation) is int
        and expectation.slot_generation >= 0
        and expectation.status is AuthorityStatus.FLAT
        and (expectation.episode_id is None
             or isinstance(expectation.episode_id, str))
    )


def _valid_now(now) -> bool:
    return (
        not isinstance(now, bool)
        and isinstance(now, (int, float))
        and math.isfinite(now)
        and now >= 0
    )


def _valid_adoption_write_plan(plan: AdoptionPlan) -> bool:
    try:
        candidate = _episode_id(plan.candidate_episode_id)
        alias = _required_text(
            plan.legacy_position_id_alias, 'legacy_position_id_alias')
    except (TypeError, ValueError):
        return False
    if not isinstance(plan.slot_key, ExchangePositionKey) \
            or candidate != plan.candidate_episode_id \
            or alias != plan.legacy_position_id_alias \
            or plan.local_symbol != plan.slot_key.symbol \
            or plan.local_side not in ('LONG', 'SHORT') \
            or plan.exchange_side != plan.local_side \
            or plan.mixed_version is not False \
            or plan.provenance is not AuthorityProvenance.MIGRATED \
            or plan.reason is not None \
            or not isinstance(plan.local_quantity, Decimal) \
            or not isinstance(plan.exchange_quantity, Decimal) \
            or not isinstance(plan.quantity_tolerance, Decimal) \
            or not plan.local_quantity.is_finite() \
            or not plan.exchange_quantity.is_finite() \
            or not plan.quantity_tolerance.is_finite() \
            or plan.local_quantity <= 0 \
            or plan.exchange_quantity <= 0 \
            or plan.quantity_tolerance < 0 \
            or abs(plan.local_quantity - plan.exchange_quantity) \
            > plan.quantity_tolerance:
        return False
    if plan.requires_authority_initialization:
        return plan.expected_authority is None \
            and plan.existing_authority is None
    expectation = plan.expected_authority
    authority = plan.existing_authority
    return (
        _valid_expectation(expectation)
        and isinstance(authority, SlotAuthority)
        and authority.exchange_position_key == plan.slot_key
        and _matches_expectation(authority, expectation)
    )


def _valid_reconstruction_write_plan(plan: ReconstructionPlan) -> bool:
    try:
        candidate = _episode_id(plan.candidate_episode_id)
    except (TypeError, ValueError):
        return False
    if not isinstance(plan.slot_key, ExchangePositionKey) \
            or candidate != plan.candidate_episode_id \
            or plan.provenance is not AuthorityProvenance.RECONSTRUCTED \
            or plan.status is not AuthorityStatus.QUARANTINED \
            or plan.exchange_side not in ('LONG', 'SHORT') \
            or plan.reason is not None \
            or not isinstance(plan.exchange_quantity, Decimal) \
            or not plan.exchange_quantity.is_finite() \
            or plan.exchange_quantity <= 0:
        return False
    if plan.requires_authority_initialization:
        return plan.expected_authority is None \
            and plan.existing_authority is None
    expectation = plan.expected_authority
    authority = plan.existing_authority
    return (
        _valid_expectation(expectation)
        and isinstance(authority, SlotAuthority)
        and authority.exchange_position_key == plan.slot_key
        and _matches_expectation(authority, expectation)
    )


def _valid_existing_legacy_plan(
        plan: AdoptionPlan, provenance: AuthorityProvenance) -> bool:
    authority = plan.existing_authority
    if not isinstance(plan.slot_key, ExchangePositionKey) \
            or not isinstance(authority, SlotAuthority) \
            or authority.exchange_position_key != plan.slot_key \
            or authority.status is not AuthorityStatus.ACTIVE \
            or authority.provenance is not provenance \
            or plan.reason is not None:
        return False
    if provenance is AuthorityProvenance.MIGRATED:
        return authority.legacy_position_id_alias \
            == plan.legacy_position_id_alias
    return True


def _valid_existing_reconstruction_plan(plan: ReconstructionPlan) -> bool:
    authority = plan.existing_authority
    return (
        isinstance(plan.slot_key, ExchangePositionKey)
        and isinstance(authority, SlotAuthority)
        and authority.exchange_position_key == plan.slot_key
        and authority.status is AuthorityStatus.QUARANTINED
        and authority.provenance is AuthorityProvenance.RECONSTRUCTED
        and plan.reason is None
    )


def _valid_legacy_nonwrite_plan(plan: AdoptionPlan) -> bool:
    if plan.expected_authority is not None \
            or plan.requires_authority_initialization:
        return False
    if plan.reason is QuarantineReason.ACCOUNT_UNKNOWN:
        return (
            plan.classification is AdoptionClassification.LEGACY_QUARANTINE
            and plan.slot_key is None
            and plan.existing_authority is None
        )
    if not isinstance(plan.slot_key, ExchangePositionKey):
        return False
    if plan.classification is AdoptionClassification.CONFLICT:
        if plan.reason is QuarantineReason.BACKEND_UNAVAILABLE:
            return plan.existing_authority is None
        return (
            plan.reason is QuarantineReason.AUTHORITY_CONFLICT
            and isinstance(plan.existing_authority, SlotAuthority)
            and plan.existing_authority.exchange_position_key == plan.slot_key
        )
    if plan.classification is AdoptionClassification.LEGACY_QUARANTINE:
        if plan.reason is QuarantineReason.AUTHORITY_CONFLICT:
            return (
                isinstance(plan.existing_authority, SlotAuthority)
                and plan.existing_authority.exchange_position_key
                == plan.slot_key
                and plan.existing_authority.status
                is AuthorityStatus.QUARANTINED
            )
        return plan.existing_authority is None and plan.reason in (
            QuarantineReason.ACCOUNT_UNKNOWN,
            QuarantineReason.SLOT_AMBIGUOUS,
            QuarantineReason.EXCHANGE_LOCAL_MISMATCH,
            QuarantineReason.AUTHORITY_MISSING,
            QuarantineReason.MIXED_VERSION,
        )
    return False


def _valid_reconstruction_nonwrite_plan(plan: ReconstructionPlan) -> bool:
    if plan.expected_authority is not None \
            or plan.requires_authority_initialization:
        return False
    if not isinstance(plan.slot_key, ExchangePositionKey):
        return False
    if plan.classification is AdoptionClassification.CONFLICT:
        if plan.reason in (
                QuarantineReason.BACKEND_UNAVAILABLE,
                QuarantineReason.AUTHORITY_MISSING):
            return plan.existing_authority is None
        return (
            plan.reason is QuarantineReason.AUTHORITY_CONFLICT
            and isinstance(plan.existing_authority, SlotAuthority)
            and plan.existing_authority.exchange_position_key == plan.slot_key
        )
    return (
        plan.classification is
        AdoptionClassification.RECONSTRUCTED_QUARANTINE
        and plan.reason is QuarantineReason.SLOT_AMBIGUOUS
        and plan.existing_authority is None
    )


def _applied_adoption_matches(
        authority: SlotAuthority | None, plan: AdoptionPlan,
        expectation: AuthorityExpectation) -> bool:
    return bool(
        isinstance(authority, SlotAuthority)
        and authority.exchange_position_key == plan.slot_key
        and authority.episode_id == plan.candidate_episode_id
        and authority.status is AuthorityStatus.ACTIVE
        and authority.provenance is AuthorityProvenance.MIGRATED
        and authority.legacy_position_id_alias
        == plan.legacy_position_id_alias
        and authority.slot_generation == expectation.slot_generation + 1
        and authority.revision == expectation.revision + 1
    )


def _applied_reconstruction_matches(
        authority: SlotAuthority | None, plan: ReconstructionPlan,
        expectation: AuthorityExpectation) -> bool:
    return bool(
        isinstance(authority, SlotAuthority)
        and authority.exchange_position_key == plan.slot_key
        and authority.episode_id == plan.candidate_episode_id
        and authority.status is AuthorityStatus.QUARANTINED
        and authority.provenance is AuthorityProvenance.RECONSTRUCTED
        and authority.slot_generation == expectation.slot_generation + 1
        and authority.revision == expectation.revision + 1
    )


def apply_legacy_adoption(
        plan: AdoptionPlan, authority_store: AuthorityStore,
        *, now: float) -> OnboardingResult:
    """Apply one prepared plan through the authority store's generation CAS."""
    if not isinstance(plan, AdoptionPlan):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
        )
    if plan.classification is AdoptionClassification.ALREADY_ADOPTED:
        if not _valid_existing_legacy_plan(
                plan, AuthorityProvenance.MIGRATED):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_AUTHORITY,
                candidate_discarded=True,
            )
        return OnboardingResult(
            OnboardingResultCode.ALREADY_ADOPTED, plan.existing_authority,
            candidate_discarded=(plan.existing_authority is not None
                                 and plan.existing_authority.episode_id
                                 != plan.candidate_episode_id),
        )
    if plan.classification is AdoptionClassification.NATIVE_AUTHORIZED:
        if not _valid_existing_legacy_plan(
                plan, AuthorityProvenance.NATIVE):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_AUTHORITY,
                candidate_discarded=True,
            )
        return OnboardingResult(
            OnboardingResultCode.EXISTING_OWNER, plan.existing_authority,
            candidate_discarded=True,
        )
    if plan.classification is AdoptionClassification.LEGACY_QUARANTINE:
        if not _valid_legacy_nonwrite_plan(plan):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_LOCAL_STATE,
                candidate_discarded=True,
            )
        return OnboardingResult(
            OnboardingResultCode.QUARANTINED, plan.existing_authority,
            plan.reason, candidate_discarded=True,
        )
    if plan.classification is AdoptionClassification.CONFLICT:
        if not _valid_legacy_nonwrite_plan(plan):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_LOCAL_STATE,
                candidate_discarded=True,
            )
        code = (OnboardingResultCode.BACKEND_ERROR
                if plan.reason is QuarantineReason.BACKEND_UNAVAILABLE
                else OnboardingResultCode.CONFLICT)
        return OnboardingResult(
            code, plan.existing_authority, plan.reason,
            candidate_discarded=True,
        )
    if plan.classification is not AdoptionClassification.LEGACY_ADOPTABLE \
            or plan.candidate_episode_id is None \
            or plan.legacy_position_id_alias is None:
        return OnboardingResult(
            OnboardingResultCode.INVALID, plan.existing_authority,
            plan.reason, candidate_discarded=True,
        )
    if not _valid_now(now) or not _valid_adoption_write_plan(plan):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            candidate_discarded=True,
        )

    expectation = plan.expected_authority
    if plan.requires_authority_initialization:
        flat, failure = _bootstrap_flat(
            slot_key=plan.slot_key, authority_store=authority_store, now=now)
        if failure is not None:
            return failure
        assert flat is not None
        if flat.status is not AuthorityStatus.FLAT:
            return _legacy_cas_resolution(plan, flat)
        if not _is_initial_flat(flat):
            return OnboardingResult(
                OnboardingResultCode.CONFLICT,
                flat,
                QuarantineReason.AUTHORITY_CONFLICT,
                candidate_discarded=True,
            )
        expectation = _expectation(flat)
    if expectation is None:
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    ack = authority_store.adopt_if_unowned(
        plan.slot_key,
        candidate_episode_id=plan.candidate_episode_id,
        legacy_position_id_alias=plan.legacy_position_id_alias,
        expected_revision=expectation.revision,
        expected_slot_generation=expectation.slot_generation,
        expected_episode_id=expectation.episode_id,
        now=now,
    )
    if ack.code is AuthorityAckCode.APPLIED \
            and _applied_adoption_matches(ack.authority, plan, expectation):
        return OnboardingResult(OnboardingResultCode.ADOPTED, ack.authority)
    current = _reload(authority_store, plan.slot_key)
    if not _valid_authority_read(current, plan.slot_key):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
            candidate_discarded=True,
            message='invalid authority reload result',
        )
    if current.code is AuthorityReadCode.FOUND and current.authority is not None:
        if ack.code is AuthorityAckCode.APPLIED \
                and _applied_adoption_matches(
                    current.authority, plan, expectation):
            return OnboardingResult(
                OnboardingResultCode.ADOPTED, current.authority)
        if ack.code is AuthorityAckCode.BACKEND_ERROR \
                and _matches_expectation(current.authority, expectation):
            return OnboardingResult(
                OnboardingResultCode.BACKEND_ERROR,
                current.authority,
                QuarantineReason.BACKEND_UNAVAILABLE,
                message=ack.message,
            )
        if ack.code is AuthorityAckCode.APPLIED:
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                current.authority,
                QuarantineReason.MALFORMED_AUTHORITY,
                candidate_discarded=True,
                message='APPLIED acknowledgement did not match adoption plan',
            )
        return _legacy_cas_resolution(plan, current.authority, expectation)
    if ack.code is AuthorityAckCode.BACKEND_ERROR \
            or current.code is AuthorityReadCode.BACKEND_ERROR:
        return OnboardingResult(
            OnboardingResultCode.BACKEND_ERROR,
            reason=QuarantineReason.BACKEND_UNAVAILABLE,
            message=ack.message or current.message,
        )
    return OnboardingResult(
        OnboardingResultCode.INVALID,
        reason=QuarantineReason.MALFORMED_AUTHORITY,
        message=ack.message or current.message,
    )


def _legacy_cas_resolution(
        plan: AdoptionPlan, authority: SlotAuthority,
        expectation: AuthorityExpectation | None = None) -> OnboardingResult:
    matches = (
        _applied_adoption_matches(authority, plan, expectation)
        if expectation is not None
        else authority.episode_id == plan.candidate_episode_id
        and authority.status is AuthorityStatus.ACTIVE
        and authority.provenance is AuthorityProvenance.MIGRATED
        and authority.legacy_position_id_alias
        == plan.legacy_position_id_alias
    )
    if matches:
        return OnboardingResult(
            OnboardingResultCode.ALREADY_ADOPTED, authority)
    return OnboardingResult(
        OnboardingResultCode.CONFLICT,
        authority,
        QuarantineReason.AUTHORITY_CONFLICT,
        candidate_discarded=True,
    )


def apply_reconstructed_episode(
        plan: ReconstructionPlan, authority_store: AuthorityStore,
        *, now: float) -> OnboardingResult:
    """CAS-create one RECONSTRUCTED + QUARANTINED authority episode."""
    if not isinstance(plan, ReconstructionPlan):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
        )
    if plan.classification is AdoptionClassification.ALREADY_RECONSTRUCTED:
        if not _valid_existing_reconstruction_plan(plan):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_AUTHORITY,
                candidate_discarded=True,
            )
        return OnboardingResult(
            OnboardingResultCode.ALREADY_RECONSTRUCTED,
            plan.existing_authority,
            candidate_discarded=(plan.existing_authority is not None
                                 and plan.existing_authority.episode_id
                                 != plan.candidate_episode_id),
        )
    if plan.classification is AdoptionClassification.CONFLICT:
        if not _valid_reconstruction_nonwrite_plan(plan):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_LOCAL_STATE,
                candidate_discarded=True,
            )
        code = (OnboardingResultCode.BACKEND_ERROR
                if plan.reason is QuarantineReason.BACKEND_UNAVAILABLE
                else OnboardingResultCode.CONFLICT)
        return OnboardingResult(
            code, plan.existing_authority, plan.reason,
            candidate_discarded=True,
        )
    if plan.classification is not \
            AdoptionClassification.RECONSTRUCTED_QUARANTINE \
            or plan.reason is not None \
            or plan.candidate_episode_id is None:
        if plan.classification is \
                AdoptionClassification.RECONSTRUCTED_QUARANTINE \
                and plan.reason is not None \
                and not _valid_reconstruction_nonwrite_plan(plan):
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                reason=QuarantineReason.MALFORMED_LOCAL_STATE,
                candidate_discarded=True,
            )
        code = (OnboardingResultCode.QUARANTINED
                if plan.classification is
                AdoptionClassification.RECONSTRUCTED_QUARANTINE
                else OnboardingResultCode.INVALID)
        return OnboardingResult(
            code, plan.existing_authority, plan.reason,
            candidate_discarded=True,
        )
    if not _valid_now(now) or not _valid_reconstruction_write_plan(plan):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_LOCAL_STATE,
            candidate_discarded=True,
        )

    expectation = plan.expected_authority
    if plan.requires_authority_initialization:
        flat, failure = _bootstrap_flat(
            slot_key=plan.slot_key, authority_store=authority_store, now=now)
        if failure is not None:
            return failure
        assert flat is not None
        if flat.status is not AuthorityStatus.FLAT:
            return _reconstruction_cas_resolution(plan, flat)
        if not _is_initial_flat(flat):
            return OnboardingResult(
                OnboardingResultCode.CONFLICT,
                flat,
                QuarantineReason.AUTHORITY_CONFLICT,
                candidate_discarded=True,
            )
        expectation = _expectation(flat)
    if expectation is None:
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
        )
    ack = authority_store.create_reconstructed_quarantined(
        plan.slot_key,
        candidate_episode_id=plan.candidate_episode_id,
        legacy_position_id_alias=None,
        expected_revision=expectation.revision,
        expected_slot_generation=expectation.slot_generation,
        expected_episode_id=expectation.episode_id,
        now=now,
    )
    if ack.code is AuthorityAckCode.APPLIED \
            and _applied_reconstruction_matches(
                ack.authority, plan, expectation):
        return OnboardingResult(
            OnboardingResultCode.RECONSTRUCTED_QUARANTINED,
            ack.authority,
        )
    current = _reload(authority_store, plan.slot_key)
    if not _valid_authority_read(current, plan.slot_key):
        return OnboardingResult(
            OnboardingResultCode.INVALID,
            reason=QuarantineReason.MALFORMED_AUTHORITY,
            candidate_discarded=True,
            message='invalid authority reload result',
        )
    if current.code is AuthorityReadCode.FOUND and current.authority is not None:
        if ack.code is AuthorityAckCode.APPLIED \
                and _applied_reconstruction_matches(
                    current.authority, plan, expectation):
            return OnboardingResult(
                OnboardingResultCode.RECONSTRUCTED_QUARANTINED,
                current.authority,
            )
        if ack.code is AuthorityAckCode.BACKEND_ERROR \
                and _matches_expectation(current.authority, expectation):
            return OnboardingResult(
                OnboardingResultCode.BACKEND_ERROR,
                current.authority,
                QuarantineReason.BACKEND_UNAVAILABLE,
                message=ack.message,
            )
        if ack.code is AuthorityAckCode.APPLIED:
            return OnboardingResult(
                OnboardingResultCode.INVALID,
                current.authority,
                QuarantineReason.MALFORMED_AUTHORITY,
                candidate_discarded=True,
                message=(
                    'APPLIED acknowledgement did not match reconstruction plan'
                ),
            )
        return _reconstruction_cas_resolution(
            plan, current.authority, expectation)
    if ack.code is AuthorityAckCode.BACKEND_ERROR \
            or current.code is AuthorityReadCode.BACKEND_ERROR:
        return OnboardingResult(
            OnboardingResultCode.BACKEND_ERROR,
            reason=QuarantineReason.BACKEND_UNAVAILABLE,
            message=ack.message or current.message,
        )
    return OnboardingResult(
        OnboardingResultCode.INVALID,
        reason=QuarantineReason.MALFORMED_AUTHORITY,
        message=ack.message or current.message,
    )


def _reconstruction_cas_resolution(
        plan: ReconstructionPlan, authority: SlotAuthority,
        expectation: AuthorityExpectation | None = None) -> OnboardingResult:
    matches = (
        _applied_reconstruction_matches(authority, plan, expectation)
        if expectation is not None
        else authority.episode_id == plan.candidate_episode_id
        and authority.status is AuthorityStatus.QUARANTINED
        and authority.provenance is AuthorityProvenance.RECONSTRUCTED
    )
    if matches:
        return OnboardingResult(
            OnboardingResultCode.ALREADY_RECONSTRUCTED, authority)
    return OnboardingResult(
        OnboardingResultCode.CONFLICT,
        authority,
        QuarantineReason.AUTHORITY_CONFLICT,
        candidate_discarded=True,
    )


def can_mutate_async(authority: SlotAuthority | None) -> bool:
    """Return whether future fenced async mutation may target this authority."""
    return bool(
        isinstance(authority, SlotAuthority)
        and authority.status is AuthorityStatus.ACTIVE
        and authority.episode_id
        and authority.slot_generation > 0
        and authority.provenance is not None
    )
