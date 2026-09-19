"""Canonical, versioned live-position projection domain (R1)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from position_identity.authority import AuthorityProvenance
from position_identity.slot import ExchangePositionKey

PROJECTION_SCHEMA_VERSION = 1


class ProjectionReadCode(str, Enum):
    FOUND = 'FOUND'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    UNAVAILABLE = 'UNAVAILABLE'


class ProjectionWriteCode(str, Enum):
    APPLIED = 'APPLIED'
    ALREADY_APPLIED = 'ALREADY_APPLIED'
    CONFLICT = 'CONFLICT'
    STALE = 'STALE'
    UNAVAILABLE = 'UNAVAILABLE'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    INVALID = 'INVALID'
    UNKNOWN = 'UNKNOWN'


def _text(value, field: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} is required')
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{field} contains control characters')
    return value


def _positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _positive_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{field} must be numeric')
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{field} must be finite and positive')
    return value


def _timestamp(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{field} must be numeric')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'{field} must be finite and nonnegative')
    return value


@dataclass(frozen=True)
class LivePositionProjection:
    """One canonical live projection; exchange remains exposure truth."""

    exchange_position_key: ExchangePositionKey
    episode_id: str
    slot_generation: int
    identity_provenance: AuthorityProvenance | str
    state_revision: int
    last_operation_id: str
    side: str
    system: str
    quantity: float
    entry_price: float
    opened_at: float
    updated_at: float
    legacy_position_id_alias: str | None = None
    schema_version: int = PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError('exchange_position_key must be ExchangePositionKey')
        if type(self.schema_version) is not int \
                or self.schema_version != PROJECTION_SCHEMA_VERSION:
            raise ValueError('unsupported projection schema version')
        try:
            episode_id = str(UUID(self.episode_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError('episode_id must be an opaque UUID') from exc
        provenance = self.identity_provenance
        if not isinstance(provenance, AuthorityProvenance):
            provenance = AuthorityProvenance(provenance)
        side = _text(self.side, 'side').upper()
        if side not in ('LONG', 'SHORT'):
            raise ValueError('side must be LONG or SHORT')
        opened_at = _timestamp(self.opened_at, 'opened_at')
        updated_at = _timestamp(self.updated_at, 'updated_at')
        if updated_at < opened_at:
            raise ValueError('updated_at must be >= opened_at')
        object.__setattr__(self, 'episode_id', episode_id)
        object.__setattr__(self, 'identity_provenance', provenance)
        object.__setattr__(self, 'slot_generation', _positive_int(
            self.slot_generation, 'slot_generation'))
        object.__setattr__(self, 'state_revision', _positive_int(
            self.state_revision, 'state_revision'))
        object.__setattr__(self, 'last_operation_id', _text(
            self.last_operation_id, 'last_operation_id'))
        object.__setattr__(self, 'side', side)
        object.__setattr__(self, 'system', _text(self.system, 'system'))
        object.__setattr__(self, 'quantity', _positive_number(
            self.quantity, 'quantity'))
        object.__setattr__(self, 'entry_price', _positive_number(
            self.entry_price, 'entry_price'))
        object.__setattr__(self, 'opened_at', opened_at)
        object.__setattr__(self, 'updated_at', updated_at)
        object.__setattr__(self, 'legacy_position_id_alias', _text(
            self.legacy_position_id_alias, 'legacy_position_id_alias',
            optional=True))

    def revise(
            self, *, operation_id: str, now: float,
            **changes) -> LivePositionProjection:
        forbidden = {
            'exchange_position_key', 'episode_id', 'slot_generation',
            'identity_provenance', 'schema_version', 'state_revision',
            'last_operation_id', 'opened_at', 'side', 'system',
            'legacy_position_id_alias',
        }
        overlap = forbidden.intersection(changes)
        if overlap:
            raise ValueError(f'immutable projection fields: {sorted(overlap)}')
        return replace(
            self, state_revision=self.state_revision + 1,
            last_operation_id=operation_id, updated_at=now, **changes)

    def to_dict(self) -> dict:
        return {
            'entry_price': self.entry_price,
            'episode_id': self.episode_id,
            'exchange_position_key': self.exchange_position_key.to_dict(),
            'identity_provenance': self.identity_provenance.value,
            'last_operation_id': self.last_operation_id,
            'legacy_position_id_alias': self.legacy_position_id_alias,
            'opened_at': self.opened_at,
            'quantity': self.quantity,
            'schema_version': self.schema_version,
            'side': self.side,
            'slot_generation': self.slot_generation,
            'state_revision': self.state_revision,
            'system': self.system,
            'updated_at': self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise TypeError('projection record must be an object')
        expected = {
            'entry_price', 'episode_id', 'exchange_position_key',
            'identity_provenance', 'last_operation_id',
            'legacy_position_id_alias', 'opened_at', 'quantity',
            'schema_version', 'side', 'slot_generation', 'state_revision',
            'system', 'updated_at',
        }
        if set(data) != expected:
            raise ValueError('invalid projection fields')
        slot = data['exchange_position_key']
        if not isinstance(slot, dict):
            raise TypeError('exchange_position_key must be an object')
        return cls(
            exchange_position_key=ExchangePositionKey(**slot),
            episode_id=data['episode_id'],
            slot_generation=data['slot_generation'],
            identity_provenance=data['identity_provenance'],
            state_revision=data['state_revision'],
            last_operation_id=data['last_operation_id'],
            side=data['side'], system=data['system'],
            quantity=data['quantity'], entry_price=data['entry_price'],
            opened_at=data['opened_at'], updated_at=data['updated_at'],
            legacy_position_id_alias=data['legacy_position_id_alias'],
            schema_version=data['schema_version'],
        )

    @classmethod
    def from_json(cls, raw: str | bytes):
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str):
            raise TypeError('projection payload must be text')
        try:
            return cls.from_dict(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError('projection payload is not valid JSON') from exc


@dataclass(frozen=True)
class ProjectionReadResult:
    code: ProjectionReadCode
    projection: LivePositionProjection | None = None
    message: str = ''


@dataclass(frozen=True)
class ProjectionWriteResult:
    code: ProjectionWriteCode
    projection: LivePositionProjection | None = None
    message: str = ''

    @property
    def applied(self) -> bool:
        return self.code in (
            ProjectionWriteCode.APPLIED,
            ProjectionWriteCode.ALREADY_APPLIED,
        )
