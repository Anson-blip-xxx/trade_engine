"""V1/V2 authority admission for asynchronous protection mutations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from position_identity import AuthorityReadCode, can_mutate_async
from position_protection.claim import (
    ClaimAcquireCode,
    ClaimValidateCode,
    ProtectionMutationClaim,
)
from position_protection.task import AlgoProtectionTask


class FenceCode(str, Enum):
    ADMITTED = 'ADMITTED'
    AUTHORITY_UNAVAILABLE = 'AUTHORITY_UNAVAILABLE'
    AUTHORITY_INVALID = 'AUTHORITY_INVALID'
    AUTHORITY_INACTIVE = 'AUTHORITY_INACTIVE'
    EPISODE_MISMATCH = 'EPISODE_MISMATCH'
    SLOT_GENERATION_MISMATCH = 'SLOT_GENERATION_MISMATCH'
    CLAIM_BUSY = 'CLAIM_BUSY'
    CLAIM_REJECTED = 'CLAIM_REJECTED'
    CLAIM_LOST = 'CLAIM_LOST'


@dataclass(frozen=True)
class FenceResult:
    code: FenceCode
    claim: ProtectionMutationClaim | None = None
    message: str = ''

    @property
    def allowed(self) -> bool:
        return self.code is FenceCode.ADMITTED and self.claim is not None


class ProtectionTaskFence:
    """Fail-closed V1 authority read/claim and V2 atomic revalidation."""

    def __init__(self, *, authority_reader, claim_store) -> None:
        self._authority_reader = authority_reader
        self._claim_store = claim_store

    def acquire(self, task: AlgoProtectionTask) -> FenceResult:
        if not isinstance(task, AlgoProtectionTask):
            raise TypeError('task must be AlgoProtectionTask')
        current = self._authority_reader.get_slot_authority(
            task.exchange_position_key)
        if current.code is AuthorityReadCode.BACKEND_ERROR:
            return FenceResult(
                FenceCode.AUTHORITY_UNAVAILABLE, message=current.message)
        if current.code is not AuthorityReadCode.FOUND \
                or current.authority is None:
            return FenceResult(
                FenceCode.AUTHORITY_INVALID, message=current.message)
        authority = current.authority
        if not can_mutate_async(authority):
            return FenceResult(FenceCode.AUTHORITY_INACTIVE)
        if authority.exchange_position_key != task.exchange_position_key \
                or authority.episode_id != task.episode_id:
            return FenceResult(FenceCode.EPISODE_MISMATCH)
        if authority.slot_generation != task.slot_generation:
            return FenceResult(FenceCode.SLOT_GENERATION_MISMATCH)
        acquired = self._claim_store.acquire(
            task, expected_authority_revision=authority.revision)
        if acquired.code is ClaimAcquireCode.ACQUIRED \
                and acquired.claim is not None:
            return FenceResult(FenceCode.ADMITTED, acquired.claim)
        if acquired.code is ClaimAcquireCode.BUSY:
            return FenceResult(FenceCode.CLAIM_BUSY)
        if acquired.code is ClaimAcquireCode.BACKEND_ERROR:
            return FenceResult(
                FenceCode.AUTHORITY_UNAVAILABLE, message=acquired.message)
        return FenceResult(FenceCode.CLAIM_REJECTED, message=acquired.message)

    def validate(self, claim: ProtectionMutationClaim) -> FenceResult:
        checked = self._claim_store.validate(claim)
        if checked.code is ClaimValidateCode.VALID:
            return FenceResult(FenceCode.ADMITTED, claim)
        if checked.code is ClaimValidateCode.BACKEND_ERROR:
            return FenceResult(
                FenceCode.AUTHORITY_UNAVAILABLE, message=checked.message)
        return FenceResult(FenceCode.CLAIM_LOST, message=checked.message)

    def release(self, claim: ProtectionMutationClaim):
        return self._claim_store.release(claim)
