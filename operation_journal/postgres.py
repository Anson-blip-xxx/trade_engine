"""Injected PostgreSQL CAS store for the dormant operation journal.

The adapter imports no database driver and opens no connection at import time.
Runtime wiring must inject a transaction-scoped connection factory.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from operation_journal.model import (
    OperationRecord,
    OperationStage,
    OperationType,
    is_legal_transition,
)
from position_identity.slot import ExchangePositionKey


class CreateCode(str, Enum):
    CREATED = "CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class ReadCode(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class CasCode(str, Enum):
    APPLIED = "APPLIED"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    UNKNOWN = "UNKNOWN"


class LeaseCode(str, Enum):
    CLAIMED = "CLAIMED"
    RENEWED = "RENEWED"
    RELEASED = "RELEASED"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    BUSY = "BUSY"
    EXPIRED = "EXPIRED"
    TERMINAL = "TERMINAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CreateResult:
    code: CreateCode
    record: OperationRecord | None = None


@dataclass(frozen=True)
class ReadResult:
    code: ReadCode
    record: OperationRecord | None = None


@dataclass(frozen=True)
class CasResult:
    code: CasCode
    record: OperationRecord | None = None


@dataclass(frozen=True)
class LeaseResult:
    code: LeaseCode
    record: OperationRecord | None = None


ConnectionFactory = Callable[[], object]

_COLUMNS = (
    "operation_id", "schema_version", "operation_type", "slot_digest",
    "exchange_position_key", "request_id", "position_episode_id",
    "lifecycle_generation", "protection_generation", "stage", "version",
    "owner_token", "lease_expires_at", "input", "exchange_aliases",
    "effect_summary", "pending_requirements", "last_error", "next_attempt_at",
    "created_at", "updated_at",
)

_RETURNING = """
RETURNING operation_id::text, schema_version, operation_type, slot_digest,
          exchange_position_key, request_id, position_episode_id::text,
          lifecycle_generation, protection_generation, stage, version,
          owner_token, EXTRACT(EPOCH FROM lease_expires_at), input,
          exchange_aliases, effect_summary, pending_requirements, last_error,
          EXTRACT(EPOCH FROM next_attempt_at), EXTRACT(EPOCH FROM created_at),
          EXTRACT(EPOCH FROM updated_at)
"""

_SELECT = """
SELECT operation_id::text, schema_version, operation_type, slot_digest,
       exchange_position_key, request_id, position_episode_id::text,
       lifecycle_generation, protection_generation, stage, version,
       owner_token, EXTRACT(EPOCH FROM lease_expires_at), input,
       exchange_aliases, effect_summary, pending_requirements, last_error,
       EXTRACT(EPOCH FROM next_attempt_at), EXTRACT(EPOCH FROM created_at),
       EXTRACT(EPOCH FROM updated_at)
FROM trade_operations
WHERE operation_id = %(operation_id)s
"""

_INSERT = """
INSERT INTO trade_operations (
    operation_id, schema_version, operation_type, slot_digest,
    exchange_position_key, request_id, position_episode_id,
    lifecycle_generation, protection_generation, stage, version, owner_token,
    lease_expires_at, input, exchange_aliases, effect_summary,
    pending_requirements, last_error, next_attempt_at, created_at, updated_at
) VALUES (
    %(operation_id)s, %(schema_version)s, %(operation_type)s, %(slot_digest)s,
    %(exchange_position_key)s::jsonb, %(request_id)s, %(position_episode_id)s,
    %(lifecycle_generation)s, %(protection_generation)s, %(stage)s, %(version)s,
    %(owner_token)s, to_timestamp(%(lease_expires_at)s), %(input)s::jsonb,
    %(exchange_aliases)s::jsonb, %(effect_summary)s::jsonb,
    %(pending_requirements)s::jsonb, %(last_error)s,
    to_timestamp(%(next_attempt_at)s), to_timestamp(%(created_at)s),
    to_timestamp(%(updated_at)s)
)
ON CONFLICT (operation_id) DO NOTHING
""" + _RETURNING

_CAS = """
UPDATE trade_operations SET
    position_episode_id = %(position_episode_id)s,
    lifecycle_generation = %(lifecycle_generation)s,
    protection_generation = %(protection_generation)s,
    stage = %(stage)s,
    version = %(version)s,
    owner_token = %(owner_token)s,
    lease_expires_at = to_timestamp(%(lease_expires_at)s),
    exchange_aliases = %(exchange_aliases)s::jsonb,
    effect_summary = %(effect_summary)s::jsonb,
    pending_requirements = %(pending_requirements)s::jsonb,
    last_error = %(last_error)s,
    next_attempt_at = to_timestamp(%(next_attempt_at)s),
    updated_at = to_timestamp(%(updated_at)s)
WHERE operation_id = %(operation_id)s
  AND version = %(expected_version)s
  AND owner_token IS NOT DISTINCT FROM %(expected_owner_token)s
  AND lease_expires_at > clock_timestamp()
""" + _RETURNING

_CLAIM_LEASE = """
UPDATE trade_operations SET
    owner_token = %(owner_token)s,
    lease_expires_at = clock_timestamp() + make_interval(secs => %(lease_seconds)s),
    version = version + 1,
    updated_at = GREATEST(updated_at, clock_timestamp())
WHERE operation_id = %(operation_id)s
  AND version = %(expected_version)s
  AND stage NOT IN ('COMPLETED', 'FAILED_TERMINAL')
  AND (owner_token IS NULL OR lease_expires_at <= clock_timestamp())
""" + _RETURNING

_RENEW_LEASE = """
UPDATE trade_operations SET
    lease_expires_at = clock_timestamp() + make_interval(secs => %(lease_seconds)s),
    version = version + 1,
    updated_at = GREATEST(updated_at, clock_timestamp())
WHERE operation_id = %(operation_id)s
  AND version = %(expected_version)s
  AND stage NOT IN ('COMPLETED', 'FAILED_TERMINAL')
  AND owner_token = %(owner_token)s
  AND lease_expires_at > clock_timestamp()
""" + _RETURNING

_RELEASE_LEASE = """
UPDATE trade_operations SET
    owner_token = NULL,
    lease_expires_at = NULL,
    version = version + 1,
    updated_at = GREATEST(updated_at, clock_timestamp())
WHERE operation_id = %(operation_id)s
  AND version = %(expected_version)s
  AND owner_token = %(owner_token)s
  AND lease_expires_at > clock_timestamp()
""" + _RETURNING


def _json(value):
    return value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _params(record: OperationRecord) -> dict:
    return {
        "operation_id": record.operation_id,
        "schema_version": record.schema_version,
        "operation_type": record.operation_type.value,
        "slot_digest": record.exchange_position_key.canonical_digest(),
        "exchange_position_key": record.exchange_position_key.to_canonical_string(),
        "request_id": record.request_id,
        "position_episode_id": record.position_episode_id,
        "lifecycle_generation": record.lifecycle_generation,
        "protection_generation": record.protection_generation,
        "stage": record.stage.value,
        "version": record.version,
        "owner_token": record.owner_token,
        "lease_expires_at": record.lease_expires_at,
        "input": record.input_json,
        "exchange_aliases": record.exchange_aliases_json,
        "effect_summary": record.effect_summary_json,
        "pending_requirements": record.pending_requirements_json,
        "last_error": record.last_error,
        "next_attempt_at": record.next_attempt_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _mapping(row) -> dict:
    if isinstance(row, dict):
        return row
    return dict(zip(_COLUMNS, row, strict=True))


def _record(row) -> OperationRecord:
    value = _mapping(row)
    key = value["exchange_position_key"]
    if isinstance(key, str):
        key = json.loads(key)
    return OperationRecord(
        operation_id=str(value["operation_id"]),
        schema_version=value["schema_version"],
        operation_type=OperationType(value["operation_type"]),
        exchange_position_key=ExchangePositionKey(**key),
        stage=OperationStage(value["stage"]),
        version=value["version"],
        input_json=_json(value["input"]),
        created_at=float(value["created_at"]),
        updated_at=float(value["updated_at"]),
        request_id=value["request_id"],
        position_episode_id=value["position_episode_id"],
        lifecycle_generation=value["lifecycle_generation"],
        protection_generation=value["protection_generation"],
        owner_token=value["owner_token"],
        lease_expires_at=(None if value["lease_expires_at"] is None else
                          float(value["lease_expires_at"])),
        exchange_aliases_json=_json(value["exchange_aliases"]),
        effect_summary_json=_json(value["effect_summary"]),
        pending_requirements_json=_json(value["pending_requirements"]),
        last_error=value["last_error"],
        next_attempt_at=(None if value["next_attempt_at"] is None else
                         float(value["next_attempt_at"])),
    )


def _lease_params(operation_id, expected_version, owner_token,
                  lease_seconds=None):
    operation_id = str(UUID(operation_id))
    if (isinstance(expected_version, bool) or
            not isinstance(expected_version, int) or expected_version < 1):
        raise ValueError("expected_version must be a positive integer")
    if (not isinstance(owner_token, str) or not owner_token or
            owner_token != owner_token.strip() or
            any(ord(char) < 32 or ord(char) == 127 for char in owner_token)):
        raise ValueError("owner_token must be normalized nonempty text")
    params = {
        "operation_id": operation_id,
        "expected_version": expected_version,
        "owner_token": owner_token,
    }
    if lease_seconds is not None:
        if (isinstance(lease_seconds, bool) or
                not isinstance(lease_seconds, (int, float)) or
                not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise ValueError("lease_seconds must be finite and positive")
        params["lease_seconds"] = float(lease_seconds)
    return params


class PostgresOperationJournal:
    """Transaction-scoped operation create/read/CAS with explicit outcomes."""

    def __init__(self, connection_factory: ConnectionFactory):
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def create(self, record: OperationRecord) -> CreateResult:
        """Insert immutable intent; any database exception is write-ambiguous."""
        if record.stage is not OperationStage.NEW or record.version != 1:
            raise ValueError("create requires a NEW version-1 operation")
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_INSERT, _params(record))
                row = cur.fetchone()
                if row is not None:
                    return CreateResult(CreateCode.CREATED, _record(row))
                cur.execute(_SELECT, {"operation_id": record.operation_id})
                existing_row = cur.fetchone()
                if existing_row is None:
                    return CreateResult(CreateCode.UNKNOWN)
                existing = _record(existing_row)
                if existing == record:
                    return CreateResult(CreateCode.ALREADY_EXISTS, existing)
                return CreateResult(CreateCode.CONFLICT, existing)
        except Exception:  # noqa: BLE001 - injected drivers expose different errors
            return CreateResult(CreateCode.UNKNOWN)

    def read(self, operation_id: str) -> ReadResult:
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_SELECT, {"operation_id": operation_id})
                row = cur.fetchone()
            if row is None:
                return ReadResult(ReadCode.NOT_FOUND)
            return ReadResult(ReadCode.FOUND, _record(row))
        except Exception:  # noqa: BLE001 - read boundary fails closed
            return ReadResult(ReadCode.UNAVAILABLE)

    def compare_and_swap(self, expected: OperationRecord,
                         desired: OperationRecord) -> CasResult:
        """Apply exactly one model transition under version and owner fencing."""
        self._validate_cas(expected, desired)
        params = _params(desired)
        params.update(expected_version=expected.version,
                      expected_owner_token=expected.owner_token)
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_CAS, params)
                row = cur.fetchone()
                if row is not None:
                    return CasResult(CasCode.APPLIED, _record(row))
                cur.execute(_SELECT, {"operation_id": expected.operation_id})
                current_row = cur.fetchone()
                if current_row is None:
                    return CasResult(CasCode.NOT_FOUND)
                current = _record(current_row)
                if current.version != expected.version:
                    return CasResult(CasCode.STALE_VERSION, current)
                if current.owner_token != expected.owner_token:
                    return CasResult(CasCode.OWNER_MISMATCH, current)
                return CasResult(CasCode.LEASE_EXPIRED, current)
        except Exception:  # noqa: BLE001 - every write error is ambiguous
            return CasResult(CasCode.UNKNOWN)

    def claim_lease(self, operation_id: str, expected_version: int,
                    owner_token: str, lease_seconds: float) -> LeaseResult:
        """Claim an unowned or database-clock-expired operation."""
        params = _lease_params(
            operation_id, expected_version, owner_token, lease_seconds)
        return self._lease_write(
            _CLAIM_LEASE, params, LeaseCode.CLAIMED, LeaseCode.BUSY,
            block_terminal=True,
        )

    def renew_lease(self, operation_id: str, expected_version: int,
                    owner_token: str, lease_seconds: float) -> LeaseResult:
        """Renew only an unexpired lease owned at the expected version."""
        params = _lease_params(
            operation_id, expected_version, owner_token, lease_seconds)
        return self._lease_write(
            _RENEW_LEASE, params, LeaseCode.RENEWED, LeaseCode.EXPIRED,
            classify_owner=True, block_terminal=True,
        )

    def release_lease(self, operation_id: str, expected_version: int,
                      owner_token: str) -> LeaseResult:
        """Release only an unexpired lease owned at the expected version."""
        params = _lease_params(operation_id, expected_version, owner_token)
        return self._lease_write(
            _RELEASE_LEASE, params, LeaseCode.RELEASED, LeaseCode.EXPIRED,
            classify_owner=True,
        )

    def _lease_write(self, sql, params, success_code, condition_code,
                     *, classify_owner=False, block_terminal=False):
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is not None:
                    return LeaseResult(success_code, _record(row))
                cur.execute(_SELECT, {"operation_id": params["operation_id"]})
                current_row = cur.fetchone()
                if current_row is None:
                    return LeaseResult(LeaseCode.NOT_FOUND)
                current = _record(current_row)
                if current.version != params["expected_version"]:
                    return LeaseResult(LeaseCode.STALE_VERSION, current)
                if block_terminal and current.stage in {
                    OperationStage.COMPLETED, OperationStage.FAILED_TERMINAL,
                }:
                    return LeaseResult(LeaseCode.TERMINAL, current)
                if classify_owner and current.owner_token != params["owner_token"]:
                    return LeaseResult(LeaseCode.OWNER_MISMATCH, current)
                return LeaseResult(condition_code, current)
        except Exception:  # noqa: BLE001 - every lease write error is ambiguous
            return LeaseResult(LeaseCode.UNKNOWN)

    @staticmethod
    def _validate_cas(expected: OperationRecord,
                      desired: OperationRecord) -> None:
        if expected.operation_id != desired.operation_id:
            raise ValueError("CAS operation_id mismatch")
        if desired.version != expected.version + 1:
            raise ValueError("CAS desired version must advance exactly once")
        if not is_legal_transition(expected.stage, desired.stage):
            raise ValueError("CAS stage transition is illegal")
        if expected.owner_token is None:
            raise ValueError("CAS requires an owned lease")
        if (desired.owner_token != expected.owner_token or
                desired.lease_expires_at != expected.lease_expires_at):
            raise ValueError("CAS cannot mutate lease ownership")
        immutable = (
            "operation_type", "exchange_position_key", "input_json",
            "request_id", "created_at", "schema_version",
        )
        if any(getattr(expected, field) != getattr(desired, field)
               for field in immutable):
            raise ValueError("CAS cannot mutate immutable operation identity")
        for field in ("position_episode_id", "lifecycle_generation",
                      "protection_generation"):
            if (getattr(expected, field) is not None and
                    getattr(expected, field) != getattr(desired, field)):
                raise ValueError(f"CAS cannot replace bound {field}")
