"""Dormant durable desired-protection domain (R2 / P10-D2A)."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from position_identity.slot import ExchangePositionKey

DESIRED_PROTECTION_SCHEMA_VERSION = 1


class ProtectionStatus(str, Enum):
    NONE = 'NONE'
    PENDING = 'PENDING'
    SUBMITTING = 'SUBMITTING'
    UNKNOWN = 'UNKNOWN'
    ACTIVE = 'ACTIVE'
    REPLACING = 'REPLACING'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    SUPERSEDED = 'SUPERSEDED'


class DesiredReadCode(str, Enum):
    FOUND = 'FOUND'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    UNAVAILABLE = 'UNAVAILABLE'


class DesiredWriteCode(str, Enum):
    APPLIED = 'APPLIED'
    ALREADY_APPLIED = 'ALREADY_APPLIED'
    STALE = 'STALE'
    CONFLICT = 'CONFLICT'
    NOT_FOUND = 'NOT_FOUND'
    MALFORMED = 'MALFORMED'
    INVALID = 'INVALID'
    UNAVAILABLE = 'UNAVAILABLE'
    UNKNOWN = 'UNKNOWN'


def _text(value, field: str) -> str:
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


_LEGAL_TRANSITIONS = {
    ProtectionStatus.PENDING: frozenset({
        ProtectionStatus.SUBMITTING, ProtectionStatus.CANCELLED,
        ProtectionStatus.SUPERSEDED, ProtectionStatus.FAILED,
    }),
    ProtectionStatus.SUBMITTING: frozenset({
        ProtectionStatus.PENDING, ProtectionStatus.UNKNOWN,
        ProtectionStatus.ACTIVE, ProtectionStatus.CANCELLED,
        ProtectionStatus.SUPERSEDED, ProtectionStatus.FAILED,
    }),
    ProtectionStatus.UNKNOWN: frozenset({
        ProtectionStatus.PENDING, ProtectionStatus.ACTIVE,
        ProtectionStatus.CANCELLED, ProtectionStatus.SUPERSEDED,
        ProtectionStatus.FAILED,
    }),
    ProtectionStatus.ACTIVE: frozenset({
        ProtectionStatus.REPLACING, ProtectionStatus.UNKNOWN,
        ProtectionStatus.CANCELLED, ProtectionStatus.SUPERSEDED,
    }),
    ProtectionStatus.REPLACING: frozenset({
        ProtectionStatus.ACTIVE, ProtectionStatus.UNKNOWN,
        ProtectionStatus.CANCELLED, ProtectionStatus.SUPERSEDED,
        ProtectionStatus.FAILED,
    }),
    ProtectionStatus.NONE: frozenset({ProtectionStatus.PENDING}),
    ProtectionStatus.FAILED: frozenset(),
    ProtectionStatus.CANCELLED: frozenset(),
    ProtectionStatus.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True)
class DesiredProtectionRecord:
    exchange_position_key: ExchangePositionKey
    episode_id: str
    slot_generation: int
    protection_generation: int
    desired_intent_id: str
    trigger_price: float
    covered_quantity: float
    closing_side: str
    status: ProtectionStatus | str
    exchange_algo_aliases: tuple[str, ...]
    revision: int
    last_operation_id: str
    created_at: float
    updated_at: float
    schema_version: int = DESIRED_PROTECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError('exchange_position_key must be ExchangePositionKey')
        if type(self.schema_version) is not int \
                or self.schema_version != DESIRED_PROTECTION_SCHEMA_VERSION:
            raise ValueError('unsupported desired-protection schema version')
        try:
            episode_id = str(UUID(self.episode_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError('episode_id must be an opaque UUID') from exc
        try:
            status = ProtectionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError('unsupported protection status') from exc
        closing_side = _text(self.closing_side, 'closing_side').upper()
        if closing_side not in ('BUY', 'SELL'):
            raise ValueError('closing_side must be BUY or SELL')
        if not isinstance(self.exchange_algo_aliases, tuple):
            raise TypeError('exchange_algo_aliases must be a tuple')
        aliases = tuple(_text(alias, 'exchange_algo_alias')
                        for alias in self.exchange_algo_aliases)
        if len(set(aliases)) != len(aliases):
            raise ValueError('exchange_algo_aliases must be unique')
        created_at = _timestamp(self.created_at, 'created_at')
        updated_at = _timestamp(self.updated_at, 'updated_at')
        if updated_at < created_at:
            raise ValueError('updated_at must be >= created_at')
        object.__setattr__(self, 'episode_id', episode_id)
        object.__setattr__(self, 'slot_generation', _positive_int(
            self.slot_generation, 'slot_generation'))
        object.__setattr__(self, 'protection_generation', _positive_int(
            self.protection_generation, 'protection_generation'))
        object.__setattr__(self, 'desired_intent_id', _text(
            self.desired_intent_id, 'desired_intent_id'))
        object.__setattr__(self, 'trigger_price', _positive_number(
            self.trigger_price, 'trigger_price'))
        object.__setattr__(self, 'covered_quantity', _positive_number(
            self.covered_quantity, 'covered_quantity'))
        object.__setattr__(self, 'closing_side', closing_side)
        object.__setattr__(self, 'status', status)
        object.__setattr__(self, 'exchange_algo_aliases', aliases)
        object.__setattr__(self, 'revision', _positive_int(
            self.revision, 'revision'))
        object.__setattr__(self, 'last_operation_id', _text(
            self.last_operation_id, 'last_operation_id'))
        object.__setattr__(self, 'created_at', created_at)
        object.__setattr__(self, 'updated_at', updated_at)

    @classmethod
    def initial_pending(
            cls, *, exchange_position_key: ExchangePositionKey,
            episode_id: str, slot_generation: int, desired_intent_id: str,
            trigger_price: float, covered_quantity: float, closing_side: str,
            operation_id: str, now: float) -> DesiredProtectionRecord:
        return cls(
            exchange_position_key=exchange_position_key,
            episode_id=episode_id, slot_generation=slot_generation,
            protection_generation=1, desired_intent_id=desired_intent_id,
            trigger_price=trigger_price, covered_quantity=covered_quantity,
            closing_side=closing_side, status=ProtectionStatus.PENDING,
            exchange_algo_aliases=(), revision=1,
            last_operation_id=operation_id, created_at=now, updated_at=now,
        )

    def same_spec(self, other: DesiredProtectionRecord) -> bool:
        return (
            isinstance(other, DesiredProtectionRecord)
            and self.exchange_position_key == other.exchange_position_key
            and self.episode_id == other.episode_id
            and self.slot_generation == other.slot_generation
            and self.trigger_price == other.trigger_price
            and self.covered_quantity == other.covered_quantity
            and self.closing_side == other.closing_side
        )

    def next_desired(
            self, *, desired_intent_id: str, trigger_price: float,
            covered_quantity: float, closing_side: str,
            operation_id: str, now: float) -> DesiredProtectionRecord:
        candidate = DesiredProtectionRecord(
            exchange_position_key=self.exchange_position_key,
            episode_id=self.episode_id,
            slot_generation=self.slot_generation,
            protection_generation=self.protection_generation + 1,
            desired_intent_id=desired_intent_id,
            trigger_price=trigger_price, covered_quantity=covered_quantity,
            closing_side=closing_side, status=ProtectionStatus.PENDING,
            exchange_algo_aliases=(), revision=self.revision + 1,
            last_operation_id=operation_id, created_at=self.created_at,
            updated_at=now,
        )
        if self.same_spec(candidate):
            raise ValueError('unchanged desired spec retains current generation')
        if candidate.desired_intent_id == self.desired_intent_id:
            raise ValueError('one desired intent cannot name different specs')
        return candidate

    def transition(
            self, *, status: ProtectionStatus | str,
            operation_id: str, now: float,
            exchange_algo_aliases: tuple[str, ...] | None = None,
            allow_same_status_alias_repair: bool = False,
            ) -> DesiredProtectionRecord:
        status = ProtectionStatus(status)
        aliases = (self.exchange_algo_aliases if exchange_algo_aliases is None
                   else exchange_algo_aliases)
        if status is self.status:
            if not allow_same_status_alias_repair or aliases == \
                    self.exchange_algo_aliases:
                raise ValueError('same-status transition requires alias repair')
        elif status not in _LEGAL_TRANSITIONS[self.status]:
            raise ValueError(
                f'illegal protection transition {self.status.value}->{status.value}')
        return DesiredProtectionRecord(
            exchange_position_key=self.exchange_position_key,
            episode_id=self.episode_id,
            slot_generation=self.slot_generation,
            protection_generation=self.protection_generation,
            desired_intent_id=self.desired_intent_id,
            trigger_price=self.trigger_price,
            covered_quantity=self.covered_quantity,
            closing_side=self.closing_side, status=status,
            exchange_algo_aliases=aliases, revision=self.revision + 1,
            last_operation_id=operation_id, created_at=self.created_at,
            updated_at=now,
        )

    def to_dict(self) -> dict:
        return {
            'closing_side': self.closing_side,
            'covered_quantity': self.covered_quantity,
            'created_at': self.created_at,
            'desired_intent_id': self.desired_intent_id,
            'episode_id': self.episode_id,
            'exchange_algo_aliases': list(self.exchange_algo_aliases),
            'exchange_position_key': self.exchange_position_key.to_dict(),
            'last_operation_id': self.last_operation_id,
            'protection_generation': self.protection_generation,
            'revision': self.revision,
            'schema_version': self.schema_version,
            'slot_generation': self.slot_generation,
            'status': self.status.value,
            'trigger_price': self.trigger_price,
            'updated_at': self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':'),
            ensure_ascii=True)

    @classmethod
    def from_dict(cls, data: dict) -> DesiredProtectionRecord:
        expected = {
            'closing_side', 'covered_quantity', 'created_at',
            'desired_intent_id', 'episode_id', 'exchange_algo_aliases',
            'exchange_position_key', 'last_operation_id',
            'protection_generation', 'revision', 'schema_version',
            'slot_generation', 'status', 'trigger_price', 'updated_at',
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError('invalid desired-protection fields')
        slot = data['exchange_position_key']
        aliases = data['exchange_algo_aliases']
        if not isinstance(slot, dict):
            raise TypeError('exchange_position_key must be an object')
        if not isinstance(aliases, list):
            raise TypeError('exchange_algo_aliases must be an array')
        return cls(
            exchange_position_key=ExchangePositionKey(**slot),
            episode_id=data['episode_id'],
            slot_generation=data['slot_generation'],
            protection_generation=data['protection_generation'],
            desired_intent_id=data['desired_intent_id'],
            trigger_price=data['trigger_price'],
            covered_quantity=data['covered_quantity'],
            closing_side=data['closing_side'], status=data['status'],
            exchange_algo_aliases=tuple(aliases), revision=data['revision'],
            last_operation_id=data['last_operation_id'],
            created_at=data['created_at'], updated_at=data['updated_at'],
            schema_version=data['schema_version'],
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> DesiredProtectionRecord:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        if not isinstance(raw, str):
            raise TypeError('desired-protection payload must be text')
        try:
            return cls.from_dict(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(
                'desired-protection payload is not valid JSON') from exc


@dataclass(frozen=True)
class DesiredReadResult:
    code: DesiredReadCode
    record: DesiredProtectionRecord | None = None
    message: str = ''


@dataclass(frozen=True)
class DesiredWriteResult:
    code: DesiredWriteCode
    record: DesiredProtectionRecord | None = None
    message: str = ''

    @property
    def applied(self) -> bool:
        return self.code in (
            DesiredWriteCode.APPLIED, DesiredWriteCode.ALREADY_APPLIED)
