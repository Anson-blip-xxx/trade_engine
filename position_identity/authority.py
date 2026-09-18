"""Immutable slot-authority domain records and typed outcomes."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum

from position_identity.slot import ExchangePositionKey

AUTHORITY_SCHEMA_VERSION = 1


class AuthorityStatus(str, Enum):
    ACTIVE = 'ACTIVE'
    FLAT = 'FLAT'
    QUARANTINED = 'QUARANTINED'


class AuthorityProvenance(str, Enum):
    NATIVE = 'NATIVE'
    MIGRATED = 'MIGRATED'
    RECONSTRUCTED = 'RECONSTRUCTED'


class AuthorityAckCode(str, Enum):
    APPLIED = 'APPLIED'
    ALREADY_ACTIVE = 'ALREADY_ACTIVE'
    ALREADY_INITIALIZED = 'ALREADY_INITIALIZED'
    CONFLICT = 'CONFLICT'
    NOT_FOUND = 'NOT_FOUND'
    INVALID_STATE = 'INVALID_STATE'
    MALFORMED = 'MALFORMED'
    BACKEND_ERROR = 'BACKEND_ERROR'


class AuthorityReadCode(str, Enum):
    FOUND = 'FOUND'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    BACKEND_ERROR = 'BACKEND_ERROR'


def _required_text(value, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{field} must be a string')
    value = value.strip()
    if not value:
        raise ValueError(f'{field} is required')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{field} contains control characters')
    return value


def _optional_text(value, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _nonnegative_int(value, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{field} must be an integer')
    if value < minimum:
        raise ValueError(f'{field} must be >= {minimum}')
    return value


def _timestamp(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{field} must be numeric')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'{field} must be finite and nonnegative')
    return value


@dataclass(frozen=True)
class SlotAuthority:
    """Current owner and retained generation high-water for one physical slot."""

    exchange_position_key: ExchangePositionKey
    episode_id: str | None
    slot_generation: int
    status: AuthorityStatus | str
    provenance: AuthorityProvenance | str | None
    revision: int
    legacy_position_id_alias: str | None
    created_at: float
    updated_at: float
    schema_version: int = AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError('exchange_position_key must be ExchangePositionKey')
        if type(self.schema_version) is not int \
                or self.schema_version != AUTHORITY_SCHEMA_VERSION:
            raise ValueError(
                f'unsupported authority schema version: {self.schema_version!r}')

        status = self.status
        if not isinstance(status, AuthorityStatus):
            try:
                status = AuthorityStatus(status)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'unsupported authority status: {status!r}') from exc

        provenance = self.provenance
        if provenance is not None and not isinstance(
                provenance, AuthorityProvenance):
            try:
                provenance = AuthorityProvenance(provenance)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f'unsupported authority provenance: {provenance!r}') from exc

        episode_id = _optional_text(self.episode_id, 'episode_id')
        legacy_alias = _optional_text(
            self.legacy_position_id_alias, 'legacy_position_id_alias')
        generation = _nonnegative_int(
            self.slot_generation, 'slot_generation')
        revision = _nonnegative_int(self.revision, 'revision', minimum=1)
        created_at = _timestamp(self.created_at, 'created_at')
        updated_at = _timestamp(self.updated_at, 'updated_at')
        if updated_at < created_at:
            raise ValueError('updated_at must be >= created_at')

        if status in (AuthorityStatus.ACTIVE, AuthorityStatus.QUARANTINED):
            if episode_id is None or provenance is None:
                raise ValueError(
                    f'{status.value} authority requires episode_id and provenance')
        elif (episode_id is None) != (provenance is None):
            raise ValueError(
                'FLAT authority episode_id and provenance must both be set or absent')

        object.__setattr__(self, 'status', status)
        object.__setattr__(self, 'provenance', provenance)
        object.__setattr__(self, 'episode_id', episode_id)
        object.__setattr__(self, 'slot_generation', generation)
        object.__setattr__(self, 'revision', revision)
        object.__setattr__(self, 'legacy_position_id_alias', legacy_alias)
        object.__setattr__(self, 'created_at', created_at)
        object.__setattr__(self, 'updated_at', updated_at)

    @classmethod
    def initial_flat(cls, exchange_position_key: ExchangePositionKey,
                     *, now: float) -> SlotAuthority:
        return cls(
            exchange_position_key=exchange_position_key,
            episode_id=None,
            slot_generation=0,
            status=AuthorityStatus.FLAT,
            provenance=None,
            revision=1,
            legacy_position_id_alias=None,
            created_at=now,
            updated_at=now,
        )

    def allocate_episode(
            self, *, episode_id: str,
            provenance: AuthorityProvenance | str,
            status: AuthorityStatus | str,
            legacy_position_id_alias: str | None,
            now: float) -> SlotAuthority:
        if self.status is not AuthorityStatus.FLAT:
            raise ValueError('new episode allocation requires FLAT authority')
        status = AuthorityStatus(status)
        if status not in (AuthorityStatus.ACTIVE, AuthorityStatus.QUARANTINED):
            raise ValueError('new episode must be ACTIVE or QUARANTINED')
        return SlotAuthority(
            exchange_position_key=self.exchange_position_key,
            episode_id=episode_id,
            slot_generation=self.slot_generation + 1,
            status=status,
            provenance=provenance,
            revision=self.revision + 1,
            legacy_position_id_alias=legacy_position_id_alias,
            created_at=self.created_at,
            updated_at=now,
        )

    def transition_to_flat(self, *, now: float) -> SlotAuthority:
        if self.status is not AuthorityStatus.ACTIVE:
            raise ValueError('flat transition requires ACTIVE authority')
        return SlotAuthority(
            exchange_position_key=self.exchange_position_key,
            episode_id=self.episode_id,
            slot_generation=self.slot_generation,
            status=AuthorityStatus.FLAT,
            provenance=self.provenance,
            revision=self.revision + 1,
            legacy_position_id_alias=self.legacy_position_id_alias,
            created_at=self.created_at,
            updated_at=now,
        )

    def to_dict(self) -> dict:
        return {
            'created_at': self.created_at,
            'episode_id': self.episode_id,
            'exchange_position_key': self.exchange_position_key.to_dict(),
            'legacy_position_id_alias': self.legacy_position_id_alias,
            'provenance': self.provenance.value if self.provenance else None,
            'revision': self.revision,
            'schema_version': self.schema_version,
            'slot_generation': self.slot_generation,
            'status': self.status.value,
            'updated_at': self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True,
        )

    @classmethod
    def from_dict(cls, data: dict) -> SlotAuthority:
        if not isinstance(data, dict):
            raise TypeError('authority record must be an object')
        expected = {
            'created_at', 'episode_id', 'exchange_position_key',
            'legacy_position_id_alias', 'provenance', 'revision',
            'schema_version', 'slot_generation', 'status', 'updated_at',
        }
        if set(data) != expected:
            missing = sorted(expected.difference(data))
            extra = sorted(set(data).difference(expected))
            raise ValueError(
                f'invalid authority fields: missing={missing}, extra={extra}')
        slot = data['exchange_position_key']
        if not isinstance(slot, dict):
            raise TypeError('exchange_position_key must be an object')
        return cls(
            exchange_position_key=ExchangePositionKey(**slot),
            episode_id=data['episode_id'],
            slot_generation=data['slot_generation'],
            status=data['status'],
            provenance=data['provenance'],
            revision=data['revision'],
            legacy_position_id_alias=data['legacy_position_id_alias'],
            created_at=data['created_at'],
            updated_at=data['updated_at'],
            schema_version=data['schema_version'],
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> SlotAuthority:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str):
            raise TypeError('authority payload must be text')
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('authority payload is not valid JSON') from exc
        return cls.from_dict(data)


@dataclass(frozen=True)
class AuthorityAck:
    code: AuthorityAckCode
    authority: SlotAuthority | None = None
    message: str = ''

    @property
    def applied(self) -> bool:
        return self.code is AuthorityAckCode.APPLIED


@dataclass(frozen=True)
class AuthorityReadResult:
    code: AuthorityReadCode
    authority: SlotAuthority | None = None
    message: str = ''
