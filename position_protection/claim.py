"""Typed mutation-claim results for fenced protection exchange effects."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from position_identity.slot import ExchangePositionKey


class ClaimAcquireCode(str, Enum):
    ACQUIRED = 'ACQUIRED'
    BUSY = 'BUSY'
    STALE = 'STALE'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    BACKEND_ERROR = 'BACKEND_ERROR'


class ClaimValidateCode(str, Enum):
    VALID = 'VALID'
    LOST = 'LOST'
    STALE = 'STALE'
    MALFORMED = 'MALFORMED'
    BACKEND_ERROR = 'BACKEND_ERROR'


class ClaimReleaseCode(str, Enum):
    RELEASED = 'RELEASED'
    NOT_OWNER = 'NOT_OWNER'
    BACKEND_ERROR = 'BACKEND_ERROR'


@dataclass(frozen=True)
class ProtectionMutationClaim:
    """One leased, fencing-token-bearing permission for a protection task."""

    exchange_position_key: ExchangePositionKey
    episode_id: str
    slot_generation: int
    protection_generation: int
    authority_revision: int
    owner_token: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError('exchange_position_key must be ExchangePositionKey')
        for field in ('episode_id', 'owner_token'):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{field} is required')
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f'{field} contains control characters')
            object.__setattr__(self, field, value.strip())
        for field in ('slot_generation', 'protection_generation',
                      'authority_revision', 'fencing_token'):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'{field} must be a positive integer')


@dataclass(frozen=True)
class ClaimAcquireResult:
    code: ClaimAcquireCode
    claim: ProtectionMutationClaim | None = None
    message: str = ''

    @property
    def acquired(self) -> bool:
        return self.code is ClaimAcquireCode.ACQUIRED and self.claim is not None


@dataclass(frozen=True)
class ClaimValidateResult:
    code: ClaimValidateCode
    message: str = ''

    @property
    def valid(self) -> bool:
        return self.code is ClaimValidateCode.VALID


@dataclass(frozen=True)
class ClaimReleaseResult:
    code: ClaimReleaseCode
    message: str = ''
