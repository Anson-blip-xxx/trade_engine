"""Backend-neutral durable operation record and legal stage transitions."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from position_identity.slot import ExchangePositionKey

OPERATION_SCHEMA_VERSION = 1


class OperationType(str, Enum):
    OPEN = "OPEN"
    CLOSE_FULL = "CLOSE_FULL"
    CLOSE_PARTIAL = "CLOSE_PARTIAL"
    PROTECTION_CREATE = "PROTECTION_CREATE"
    PROTECTION_REPLACE = "PROTECTION_REPLACE"
    GHOST_FINALIZE = "GHOST_FINALIZE"
    EXTERNAL_RECONCILE = "EXTERNAL_RECONCILE"


class OperationStage(str, Enum):
    NEW = "NEW"
    INTENT_DURABLE = "INTENT_DURABLE"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    EXCHANGE_ACKED = "EXCHANGE_ACKED"
    EFFECT_CONFIRMED = "EFFECT_CONFIRMED"
    LOCAL_PROJECTED = "LOCAL_PROJECTED"
    AUXILIARY_PENDING = "AUXILIARY_PENDING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    COMPENSATION_REQUIRED = "COMPENSATION_REQUIRED"


_LEGAL = {
    OperationStage.NEW: {OperationStage.INTENT_DURABLE, OperationStage.FAILED_TERMINAL},
    OperationStage.INTENT_DURABLE: {
        OperationStage.SUBMITTING, OperationStage.FAILED_RETRYABLE,
        OperationStage.FAILED_TERMINAL,
    },
    OperationStage.SUBMITTING: {
        OperationStage.UNKNOWN, OperationStage.EXCHANGE_ACKED,
        OperationStage.EFFECT_CONFIRMED, OperationStage.FAILED_RETRYABLE,
        OperationStage.FAILED_TERMINAL,
    },
    OperationStage.UNKNOWN: {
        OperationStage.EXCHANGE_ACKED, OperationStage.EFFECT_CONFIRMED,
        OperationStage.FAILED_RETRYABLE, OperationStage.FAILED_TERMINAL,
    },
    OperationStage.EXCHANGE_ACKED: {
        OperationStage.UNKNOWN, OperationStage.EFFECT_CONFIRMED,
    },
    OperationStage.EFFECT_CONFIRMED: {
        OperationStage.LOCAL_PROJECTED, OperationStage.COMPENSATION_REQUIRED,
    },
    OperationStage.LOCAL_PROJECTED: {
        OperationStage.AUXILIARY_PENDING, OperationStage.COMPLETED,
        OperationStage.COMPENSATION_REQUIRED,
    },
    OperationStage.AUXILIARY_PENDING: {
        OperationStage.COMPLETED, OperationStage.FAILED_TERMINAL,
    },
    OperationStage.FAILED_RETRYABLE: {
        OperationStage.INTENT_DURABLE, OperationStage.SUBMITTING,
    },
    OperationStage.COMPENSATION_REQUIRED: {OperationStage.COMPLETED},
    OperationStage.COMPLETED: set(),
    OperationStage.FAILED_TERMINAL: set(),
}

_ABSENCE_PROOF_REQUIRED = frozenset(
    {
        (OperationStage.SUBMITTING, OperationStage.FAILED_RETRYABLE),
        (OperationStage.SUBMITTING, OperationStage.FAILED_TERMINAL),
        (OperationStage.UNKNOWN, OperationStage.FAILED_RETRYABLE),
        (OperationStage.UNKNOWN, OperationStage.FAILED_TERMINAL),
    }
)


def is_legal_transition(source: OperationStage, target: OperationStage) -> bool:
    if not isinstance(source, OperationStage) or not isinstance(target, OperationStage):
        raise TypeError("source and target must be OperationStage")
    return target in _LEGAL[source]


def _text(value, field, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value.strip()


def _time(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value


def canonical_json(value, field):
    if not isinstance(value, (dict, list)):
        raise TypeError(f"{field} must be an object or array")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON-safe") from exc


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    operation_type: OperationType
    exchange_position_key: ExchangePositionKey
    stage: OperationStage
    version: int
    input_json: str
    created_at: float
    updated_at: float
    request_id: str | None = None
    position_episode_id: str | None = None
    lifecycle_generation: int | None = None
    protection_generation: int | None = None
    owner_token: str | None = None
    lease_expires_at: float | None = None
    exchange_aliases_json: str = "{}"
    effect_summary_json: str = "{}"
    pending_requirements_json: str = "[]"
    last_error: str | None = None
    next_attempt_at: float | None = None
    schema_version: int = OPERATION_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != OPERATION_SCHEMA_VERSION:
            raise ValueError("unsupported operation schema version")
        object.__setattr__(self, "operation_id", str(UUID(self.operation_id)))
        if not isinstance(self.operation_type, OperationType):
            raise TypeError("operation_type must be OperationType")
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        if not isinstance(self.stage, OperationStage):
            raise TypeError("stage must be OperationStage")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        for field in ("input_json", "exchange_aliases_json", "effect_summary_json", "pending_requirements_json"):
            value = getattr(self, field)
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{field} must be valid JSON") from exc
            object.__setattr__(self, field, canonical_json(parsed, field))
        created = _time(self.created_at, "created_at")
        updated = _time(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at must be >= created_at")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        for field in ("request_id", "owner_token", "last_error"):
            object.__setattr__(self, field, _text(getattr(self, field), field, optional=True))
        if self.position_episode_id is not None:
            object.__setattr__(self, "position_episode_id", str(UUID(self.position_episode_id)))
        for field in ("lifecycle_generation", "protection_generation"):
            value = getattr(self, field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{field} must be a positive integer")
        for field in ("lease_expires_at", "next_attempt_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _time(value, field))
        if (self.owner_token is None) != (self.lease_expires_at is None):
            raise ValueError("owner_token and lease_expires_at must be present together")

    @classmethod
    def new(cls, *, operation_id, operation_type, exchange_position_key,
            normalized_input, now, request_id=None):
        return cls(
            operation_id=operation_id, operation_type=operation_type,
            exchange_position_key=exchange_position_key, stage=OperationStage.NEW,
            version=1, input_json=canonical_json(normalized_input, "normalized_input"),
            created_at=now, updated_at=now, request_id=request_id,
        )

    def transition(self, *, stage, now, effect_absence_proven=False, **changes):
        if not isinstance(stage, OperationStage):
            raise TypeError("stage must be OperationStage")
        if not is_legal_transition(self.stage, stage):
            raise ValueError(f"illegal operation transition {self.stage.value}->{stage.value}")
        if (self.stage, stage) in _ABSENCE_PROOF_REQUIRED and effect_absence_proven is not True:
            raise ValueError("failure after submission ambiguity requires effect-absence proof")
        immutable = {"operation_id", "operation_type", "exchange_position_key", "input_json", "request_id", "created_at", "schema_version"}
        if immutable.intersection(changes):
            raise ValueError("immutable operation fields cannot change")
        for field in ("position_episode_id", "lifecycle_generation",
                      "protection_generation"):
            if (field in changes and getattr(self, field) is not None and
                    changes[field] != getattr(self, field)):
                raise ValueError(f"bound {field} cannot change")
        return replace(self, stage=stage, version=self.version + 1, updated_at=now, **changes)
