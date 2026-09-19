"""Immutable queued Algo-protection work identity (P10-D3D-1B).

This module defines queue payload shape and legacy classification only. It does
not read slot authority or authorize an exchange mutation; worker preflight
belongs to P10-D3D-1C.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from position_identity.slot import ExchangePositionKey


class QueueTaskClassification(str, Enum):
    FENCED = 'FENCED'
    LEGACY_UNFENCED = 'LEGACY_UNFENCED'
    MALFORMED = 'MALFORMED'


@dataclass(frozen=True)
class AlgoProtectionTask:
    """One immutable desired protection mutation for a canonical episode."""

    symbol: str
    side: str
    trigger_price: float
    qty: float
    exchange_position_key: ExchangePositionKey
    episode_id: str
    slot_generation: int
    protection_generation: int

    def __post_init__(self) -> None:
        symbol = self.symbol
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError('symbol is required')
        symbol = symbol.strip().upper()
        if any(ord(char) <= 32 or ord(char) == 127 for char in symbol):
            raise ValueError('symbol contains whitespace or control characters')

        side = self.side
        if not isinstance(side, str) or side.strip().upper() not in ('BUY', 'SELL'):
            raise ValueError('side must be BUY or SELL')
        side = side.strip().upper()

        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError(
                'exchange_position_key must be ExchangePositionKey')
        if self.exchange_position_key.symbol != symbol:
            raise ValueError('symbol must match exchange_position_key')

        try:
            episode_id = str(UUID(self.episode_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError('episode_id must be an opaque UUID') from exc

        trigger = _positive_number(self.trigger_price, 'trigger_price')
        qty = _positive_number(self.qty, 'qty')
        slot_generation = _positive_int(
            self.slot_generation, 'slot_generation')
        protection_generation = _positive_int(
            self.protection_generation, 'protection_generation')

        object.__setattr__(self, 'symbol', symbol)
        object.__setattr__(self, 'side', side)
        object.__setattr__(self, 'trigger_price', trigger)
        object.__setattr__(self, 'qty', qty)
        object.__setattr__(self, 'episode_id', episode_id)
        object.__setattr__(self, 'slot_generation', slot_generation)
        object.__setattr__(
            self, 'protection_generation', protection_generation)


@dataclass(frozen=True)
class ConditionalWritebackProtectionTask(AlgoProtectionTask):
    """V3 task carrying revisions required for conditional ACK writeback."""

    desired_revision: int
    projection_revision: int

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, 'desired_revision', _positive_int(
            self.desired_revision, 'desired_revision'))
        object.__setattr__(self, 'projection_revision', _positive_int(
            self.projection_revision, 'projection_revision'))


@dataclass(frozen=True)
class QueueTaskParseResult:
    classification: QueueTaskClassification
    task: AlgoProtectionTask | None = None
    symbol: str | None = None


def _positive_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'{field} must be numeric')
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{field} must be finite and positive')
    return value


def _positive_int(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{field} must be an integer')
    if value < 1:
        raise ValueError(f'{field} must be >= 1')
    return value


def classify_queue_task(value) -> QueueTaskParseResult:
    """Classify without deriving missing identity from current slot state."""
    if isinstance(value, AlgoProtectionTask):
        return QueueTaskParseResult(
            QueueTaskClassification.FENCED,
            task=value,
            symbol=value.symbol,
        )
    if isinstance(value, tuple) and len(value) == 4:
        symbol = value[0] if isinstance(value[0], str) else None
        return QueueTaskParseResult(
            QueueTaskClassification.LEGACY_UNFENCED,
            symbol=symbol,
        )
    return QueueTaskParseResult(QueueTaskClassification.MALFORMED)
