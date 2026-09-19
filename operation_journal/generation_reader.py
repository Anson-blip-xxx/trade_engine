"""Injected read-only coordinator for the D3D recovery generation fence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from operation_journal.generation_fence import (
    GenerationFenceDecision,
    validate_recovery_generation,
)
from operation_journal.model import OperationRecord, OperationType
from position_identity.authority import (
    AuthorityReadCode,
    AuthorityReadResult,
    SlotAuthority,
)
from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    DesiredReadCode,
    DesiredReadResult,
)


class AuthorityReader(Protocol):
    def get_slot_authority(
            self, key: ExchangePositionKey) -> AuthorityReadResult: ...


class DesiredReader(Protocol):
    def get(self, key: ExchangePositionKey) -> DesiredReadResult: ...


class GenerationResolutionCode(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    AUTHORITY_NOT_FOUND = "AUTHORITY_NOT_FOUND"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    AUTHORITY_MALFORMED = "AUTHORITY_MALFORMED"
    DESIRED_NOT_FOUND = "DESIRED_NOT_FOUND"
    DESIRED_UNAVAILABLE = "DESIRED_UNAVAILABLE"
    DESIRED_MALFORMED = "DESIRED_MALFORMED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_WITNESS = "INVALID_WITNESS"
    CANONICAL_CHANGED = "CANONICAL_CHANGED"


@dataclass(frozen=True)
class GenerationWitness:
    """Exact canonical records observed by a successful generation read."""

    operation_id: str
    operation_type: OperationType
    authority: SlotAuthority
    desired: DesiredProtectionRecord | None


@dataclass(frozen=True)
class GenerationResolution:
    code: GenerationResolutionCode
    decision: GenerationFenceDecision | None = None
    message: str = ""
    witness: GenerationWitness | None = None


_PROTECTION_TYPES = frozenset({
    OperationType.PROTECTION_CREATE,
    OperationType.PROTECTION_REPLACE,
})


class RecoveryGenerationReader:
    """Read canonical records once, then apply the pure generation fence."""

    def __init__(self, authority_reader: AuthorityReader,
                 desired_reader: DesiredReader | None = None):
        if not callable(getattr(authority_reader, "get_slot_authority", None)):
            raise TypeError("authority_reader must provide get_slot_authority")
        if (desired_reader is not None and
                not callable(getattr(desired_reader, "get", None))):
            raise TypeError("desired_reader must provide get")
        self._authority_reader = authority_reader
        self._desired_reader = desired_reader

    def resolve(self, record: OperationRecord) -> GenerationResolution:
        if not isinstance(record, OperationRecord):
            raise TypeError("record must be OperationRecord")
        try:
            authority_result = self._authority_reader.get_slot_authority(
                record.exchange_position_key)
        except Exception as exc:  # noqa: BLE001 - injected read boundary
            return GenerationResolution(
                GenerationResolutionCode.AUTHORITY_UNAVAILABLE,
                message=str(exc),
            )
        authority_resolution = self._authority_result(authority_result)
        if isinstance(authority_resolution, GenerationResolution):
            return authority_resolution
        authority = authority_resolution

        desired = None
        if record.operation_type in _PROTECTION_TYPES:
            if self._desired_reader is None:
                return GenerationResolution(
                    GenerationResolutionCode.DESIRED_UNAVAILABLE,
                    message="desired reader is not configured",
                )
            try:
                desired_result = self._desired_reader.get(
                    record.exchange_position_key)
            except Exception as exc:  # noqa: BLE001 - injected read boundary
                return GenerationResolution(
                    GenerationResolutionCode.DESIRED_UNAVAILABLE,
                    message=str(exc),
                )
            desired_resolution = self._desired_result(desired_result)
            if isinstance(desired_resolution, GenerationResolution):
                return desired_resolution
            desired = desired_resolution

        decision = validate_recovery_generation(record, authority, desired)
        code = (GenerationResolutionCode.PASSED if decision.fence_passed
                else GenerationResolutionCode.BLOCKED)
        witness = None
        if code is GenerationResolutionCode.PASSED:
            witness = GenerationWitness(
                operation_id=record.operation_id,
                operation_type=record.operation_type,
                authority=authority,
                desired=desired,
            )
        return GenerationResolution(code, decision, witness=witness)

    def revalidate(
            self,
            record: OperationRecord,
            expected: GenerationWitness,
            ) -> GenerationResolution:
        """Re-read canonical state and require exact witness equality."""
        if not isinstance(record, OperationRecord):
            raise TypeError("record must be OperationRecord")
        if not isinstance(expected, GenerationWitness):
            raise TypeError("expected must be GenerationWitness")
        if (expected.operation_id != record.operation_id or
                expected.operation_type is not record.operation_type):
            return GenerationResolution(
                GenerationResolutionCode.INVALID_WITNESS,
                message="witness belongs to a different operation",
            )
        current = self.resolve(record)
        if current.code is not GenerationResolutionCode.PASSED:
            return current
        if current.witness != expected:
            return GenerationResolution(
                GenerationResolutionCode.CANONICAL_CHANGED,
                decision=current.decision,
                message="canonical records changed since the planning read",
                witness=current.witness,
            )
        return current

    @staticmethod
    def _authority_result(result):
        if not isinstance(result, AuthorityReadResult):
            return GenerationResolution(
                GenerationResolutionCode.INVALID_RESPONSE,
                message="authority reader returned an invalid response",
            )
        if result.code is AuthorityReadCode.NOT_FOUND:
            return GenerationResolution(
                GenerationResolutionCode.AUTHORITY_NOT_FOUND,
                message=result.message,
            )
        if result.code is AuthorityReadCode.MALFORMED:
            return GenerationResolution(
                GenerationResolutionCode.AUTHORITY_MALFORMED,
                message=result.message,
            )
        if result.code is AuthorityReadCode.BACKEND_ERROR:
            return GenerationResolution(
                GenerationResolutionCode.AUTHORITY_UNAVAILABLE,
                message=result.message,
            )
        if result.code is not AuthorityReadCode.FOUND or result.authority is None:
            return GenerationResolution(
                GenerationResolutionCode.INVALID_RESPONSE,
                message="authority FOUND response omitted authority",
            )
        return result.authority

    @staticmethod
    def _desired_result(result):
        if not isinstance(result, DesiredReadResult):
            return GenerationResolution(
                GenerationResolutionCode.INVALID_RESPONSE,
                message="desired reader returned an invalid response",
            )
        if result.code is DesiredReadCode.NOT_FOUND:
            return GenerationResolution(
                GenerationResolutionCode.DESIRED_NOT_FOUND,
                message=result.message,
            )
        if result.code is DesiredReadCode.MALFORMED:
            return GenerationResolution(
                GenerationResolutionCode.DESIRED_MALFORMED,
                message=result.message,
            )
        if result.code is DesiredReadCode.UNAVAILABLE:
            return GenerationResolution(
                GenerationResolutionCode.DESIRED_UNAVAILABLE,
                message=result.message,
            )
        if result.code is not DesiredReadCode.FOUND or result.record is None:
            return GenerationResolution(
                GenerationResolutionCode.INVALID_RESPONSE,
                message="desired FOUND response omitted record",
            )
        return result.record
