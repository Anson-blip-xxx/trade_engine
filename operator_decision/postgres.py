"""Injected PostgreSQL adapter for the dormant operator decision center.

No driver, DSN, environment variable, worker, or transport is imported here.
Runtime wiring must inject a transaction-scoped connection factory.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from operation_journal.delayed_policy import DelayedAction, DelayedEventKind
from operator_decision.model import (
    DecisionItem,
    DecisionResolution,
    DecisionSeverity,
    DecisionStatus,
    NotificationChannel,
    NotificationOutboxItem,
    NotificationStatus,
    is_legal_decision_transition,
)
from position_identity.slot import ExchangePositionKey


class CreateDecisionCode(str, Enum):
    CREATED = "CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class DecisionReadCode(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"


class DecisionCasCode(str, Enum):
    APPLIED = "APPLIED"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    UNKNOWN = "UNKNOWN"


class NotificationClaimCode(str, Enum):
    CLAIMED = "CLAIMED"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


class NotificationCasCode(str, Enum):
    APPLIED = "APPLIED"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CreateDecisionResult:
    code: CreateDecisionCode
    decision: DecisionItem | None = None
    notification: NotificationOutboxItem | None = None


@dataclass(frozen=True)
class DecisionReadResult:
    code: DecisionReadCode
    decision: DecisionItem | None = None


@dataclass(frozen=True)
class DecisionCasResult:
    code: DecisionCasCode
    decision: DecisionItem | None = None


@dataclass(frozen=True)
class InboxResult:
    code: DecisionReadCode
    decisions: tuple[DecisionItem, ...] = ()


@dataclass(frozen=True)
class NotificationClaimResult:
    code: NotificationClaimCode
    notifications: tuple[NotificationOutboxItem, ...] = ()


@dataclass(frozen=True)
class NotificationCasResult:
    code: NotificationCasCode
    notification: NotificationOutboxItem | None = None


ConnectionFactory = Callable[[], object]

_DECISION_COLUMNS = (
    "decision_id", "schema_version", "event_id", "operation_id",
    "slot_digest", "exchange_position_key", "event_kind", "severity",
    "status", "version", "trigger_context", "current_context",
    "trigger_context_digest", "current_context_digest", "fallback_action",
    "fallback_at", "approval_expires_at", "assigned_to", "resolution",
    "resolution_detail", "created_at", "updated_at",
)
_NOTIFICATION_COLUMNS = (
    "notification_id", "schema_version", "decision_id", "channel",
    "dedupe_key", "status", "version", "payload", "attempt_count",
    "next_attempt_at", "owner_token", "lease_expires_at", "delivered_at",
    "last_error", "created_at", "updated_at",
)

_DECISION_RETURNING = """
RETURNING decision_id::text, schema_version, event_id::text,
          operation_id::text, slot_digest, exchange_position_key, event_kind,
          severity, status, version, trigger_context, current_context,
          trigger_context_digest, current_context_digest, fallback_action,
          EXTRACT(EPOCH FROM fallback_at),
          EXTRACT(EPOCH FROM approval_expires_at), assigned_to, resolution,
          resolution_detail, EXTRACT(EPOCH FROM created_at),
          EXTRACT(EPOCH FROM updated_at)
"""
_NOTIFICATION_RETURNING = """
RETURNING notification_id::text, schema_version, decision_id::text, channel,
          dedupe_key, status, version, payload, attempt_count,
          EXTRACT(EPOCH FROM next_attempt_at), owner_token,
          EXTRACT(EPOCH FROM lease_expires_at),
          EXTRACT(EPOCH FROM delivered_at), last_error,
          EXTRACT(EPOCH FROM created_at), EXTRACT(EPOCH FROM updated_at)
"""
_NOTIFICATION_RETURNING_TARGET = _NOTIFICATION_RETURNING.replace(
    "RETURNING notification_id::text",
    "RETURNING target.notification_id::text",
)

_INSERT_DECISION = """
INSERT INTO operator_decisions (
    decision_id, schema_version, event_id, operation_id, slot_digest,
    exchange_position_key, event_kind, severity, status, version,
    trigger_context, current_context, trigger_context_digest,
    current_context_digest, fallback_action, fallback_at,
    approval_expires_at, assigned_to, resolution, resolution_detail,
    created_at, updated_at
) VALUES (
    %(decision_id)s, %(schema_version)s, %(event_id)s, %(operation_id)s,
    %(slot_digest)s, %(exchange_position_key)s::jsonb, %(event_kind)s,
    %(severity)s, %(status)s, %(version)s, %(trigger_context)s::jsonb,
    %(current_context)s::jsonb, %(trigger_context_digest)s,
    %(current_context_digest)s, %(fallback_action)s,
    to_timestamp(%(fallback_at)s), to_timestamp(%(approval_expires_at)s),
    %(assigned_to)s, %(resolution)s, %(resolution_detail)s::jsonb,
    to_timestamp(%(created_at)s), to_timestamp(%(updated_at)s)
)
ON CONFLICT (decision_id) DO NOTHING
""" + _DECISION_RETURNING

_INSERT_NOTIFICATION = """
INSERT INTO operator_notification_outbox (
    notification_id, schema_version, decision_id, channel, dedupe_key,
    status, version, payload, attempt_count, next_attempt_at, owner_token,
    lease_expires_at, delivered_at, last_error, created_at, updated_at
) VALUES (
    %(notification_id)s, %(schema_version)s, %(decision_id)s, %(channel)s,
    %(dedupe_key)s, %(status)s, %(version)s, %(payload)s::jsonb,
    %(attempt_count)s, to_timestamp(%(next_attempt_at)s), %(owner_token)s,
    to_timestamp(%(lease_expires_at)s), to_timestamp(%(delivered_at)s),
    %(last_error)s, to_timestamp(%(created_at)s), to_timestamp(%(updated_at)s)
)
""" + _NOTIFICATION_RETURNING

_SELECT_DECISION = _DECISION_RETURNING.replace(
    "RETURNING", "SELECT").replace(
    "\n", "\n", 1) + "FROM operator_decisions WHERE decision_id = %(decision_id)s"
_SELECT_NOTIFICATION = _NOTIFICATION_RETURNING.replace(
    "RETURNING", "SELECT").replace(
    "\n", "\n", 1) + (
        "FROM operator_notification_outbox "
        "WHERE notification_id = %(notification_id)s"
    )

_LIST_INBOX = _DECISION_RETURNING.replace("RETURNING", "SELECT") + """
FROM operator_decisions
WHERE status IN ('OPEN','ACKNOWLEDGED')
ORDER BY CASE severity
    WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
    WHEN 'WARNING' THEN 2 ELSE 3 END,
    fallback_at, created_at, decision_id
LIMIT %(limit)s
"""

_CAS_DECISION = """
UPDATE operator_decisions SET
    status = %(status)s, version = %(version)s,
    current_context = %(current_context)s::jsonb,
    current_context_digest = %(current_context_digest)s,
    assigned_to = %(assigned_to)s, resolution = %(resolution)s,
    resolution_detail = %(resolution_detail)s::jsonb,
    updated_at = to_timestamp(%(updated_at)s)
WHERE decision_id = %(decision_id)s AND version = %(expected_version)s
""" + _DECISION_RETURNING

_CLAIM_NOTIFICATIONS = """
WITH now_value AS MATERIALIZED (
    SELECT clock_timestamp() AS now
), candidates AS MATERIALIZED (
    SELECT item.notification_id
    FROM operator_notification_outbox AS item, now_value
    WHERE ((item.status IN ('PENDING','RETRY_WAIT')
            AND item.next_attempt_at <= now_value.now)
           OR (item.status = 'CLAIMED'
               AND item.lease_expires_at <= now_value.now))
    ORDER BY CASE WHEN item.status = 'CLAIMED'
                  THEN item.lease_expires_at ELSE item.next_attempt_at END,
             item.created_at, item.notification_id
    LIMIT %(limit)s
    FOR UPDATE OF item SKIP LOCKED
)
UPDATE operator_notification_outbox AS target SET
    status = 'CLAIMED', owner_token = %(owner_token)s,
    lease_expires_at = now_value.now + make_interval(secs => %(lease_seconds)s),
    attempt_count = target.attempt_count + 1,
    version = target.version + 1,
    updated_at = GREATEST(target.updated_at, now_value.now)
FROM candidates, now_value
WHERE target.notification_id = candidates.notification_id
""" + _NOTIFICATION_RETURNING_TARGET

_CAS_NOTIFICATION = """
UPDATE operator_notification_outbox SET
    status = %(status)s, version = %(version)s,
    next_attempt_at = to_timestamp(%(next_attempt_at)s),
    owner_token = %(owner_token)s,
    lease_expires_at = to_timestamp(%(lease_expires_at)s),
    delivered_at = to_timestamp(%(delivered_at)s),
    last_error = %(last_error)s,
    updated_at = to_timestamp(%(updated_at)s)
WHERE notification_id = %(notification_id)s
  AND version = %(expected_version)s
  AND status = 'CLAIMED'
  AND owner_token = %(expected_owner_token)s
  AND lease_expires_at > clock_timestamp()
""" + _NOTIFICATION_RETURNING


def _json(value):
    return value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _mapping(row, columns):
    if isinstance(row, dict):
        return row
    return dict(zip(columns, row, strict=True))


def _optional_float(value):
    return None if value is None else float(value)


def _decision_params(item):
    return {
        "decision_id": item.decision_id,
        "schema_version": item.schema_version,
        "event_id": item.event_id,
        "operation_id": item.operation_id,
        "slot_digest": item.exchange_position_key.canonical_digest(),
        "exchange_position_key": item.exchange_position_key.to_canonical_string(),
        "event_kind": item.event_kind.value,
        "severity": item.severity.value,
        "status": item.status.value,
        "version": item.version,
        "trigger_context": item.trigger_context_json,
        "current_context": item.current_context_json,
        "trigger_context_digest": item.trigger_context_digest,
        "current_context_digest": item.current_context_digest,
        "fallback_action": item.fallback_action.value,
        "fallback_at": item.fallback_at,
        "approval_expires_at": item.approval_expires_at,
        "assigned_to": item.assigned_to,
        "resolution": None if item.resolution is None else item.resolution.value,
        "resolution_detail": item.resolution_json,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _notification_params(item):
    return {
        "notification_id": item.notification_id,
        "schema_version": item.schema_version,
        "decision_id": item.decision_id,
        "channel": item.channel.value,
        "dedupe_key": item.dedupe_key,
        "status": item.status.value,
        "version": item.version,
        "payload": item.payload_json,
        "attempt_count": item.attempt_count,
        "next_attempt_at": item.next_attempt_at,
        "owner_token": item.owner_token,
        "lease_expires_at": item.lease_expires_at,
        "delivered_at": item.delivered_at,
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _decision(row):
    value = _mapping(row, _DECISION_COLUMNS)
    key = value["exchange_position_key"]
    if isinstance(key, str):
        key = json.loads(key)
    resolution = value["resolution"]
    return DecisionItem(
        decision_id=str(value["decision_id"]), event_id=str(value["event_id"]),
        operation_id=(None if value["operation_id"] is None else
                      str(value["operation_id"])),
        exchange_position_key=ExchangePositionKey(**key),
        event_kind=DelayedEventKind(value["event_kind"]),
        severity=DecisionSeverity(value["severity"]),
        status=DecisionStatus(value["status"]), version=value["version"],
        trigger_context_json=_json(value["trigger_context"]),
        current_context_json=_json(value["current_context"]),
        trigger_context_digest=value["trigger_context_digest"],
        current_context_digest=value["current_context_digest"],
        fallback_action=DelayedAction(value["fallback_action"]),
        fallback_at=float(value["fallback_at"]),
        approval_expires_at=_optional_float(value["approval_expires_at"]),
        assigned_to=value["assigned_to"],
        resolution=(None if resolution is None else
                    DecisionResolution(resolution)),
        resolution_json=_json(value["resolution_detail"]),
        created_at=float(value["created_at"]),
        updated_at=float(value["updated_at"]),
        schema_version=value["schema_version"],
    )


def _notification(row):
    value = _mapping(row, _NOTIFICATION_COLUMNS)
    return NotificationOutboxItem(
        notification_id=str(value["notification_id"]),
        decision_id=str(value["decision_id"]),
        channel=NotificationChannel(value["channel"]),
        dedupe_key=value["dedupe_key"],
        status=NotificationStatus(value["status"]), version=value["version"],
        payload_json=_json(value["payload"]),
        attempt_count=value["attempt_count"],
        next_attempt_at=float(value["next_attempt_at"]),
        owner_token=value["owner_token"],
        lease_expires_at=_optional_float(value["lease_expires_at"]),
        delivered_at=_optional_float(value["delivered_at"]),
        last_error=value["last_error"],
        created_at=float(value["created_at"]),
        updated_at=float(value["updated_at"]),
        schema_version=value["schema_version"],
    )


def _positive_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer between 1 and 500")
    return limit


def _owner(owner_token):
    if (not isinstance(owner_token, str) or not owner_token.strip() or
            owner_token != owner_token.strip() or
            any(ord(char) < 32 or ord(char) == 127 for char in owner_token)):
        raise ValueError("owner_token must be normalized nonempty text")
    return owner_token


def _lease_seconds(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or value <= 0):
        raise ValueError("lease_seconds must be finite and positive")
    return float(value)


class PostgresOperatorDecisionStore:
    """Transaction-scoped inbox/outbox persistence with explicit outcomes."""

    def __init__(self, connection_factory: ConnectionFactory):
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._connection_factory = connection_factory

    def create_with_notification(self, decision, notification):
        if decision.status is not DecisionStatus.OPEN or decision.version != 1:
            raise ValueError("create requires an OPEN version-1 decision")
        if (notification.status is not NotificationStatus.PENDING or
                notification.version != 1 or notification.attempt_count != 0):
            raise ValueError("create requires a PENDING version-1 notification")
        if notification.decision_id != decision.decision_id:
            raise ValueError("notification decision_id mismatch")
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_INSERT_DECISION, _decision_params(decision))
                inserted = cur.fetchone()
                if inserted is not None:
                    cur.execute(_INSERT_NOTIFICATION,
                                _notification_params(notification))
                    notification_row = cur.fetchone()
                    return CreateDecisionResult(
                        CreateDecisionCode.CREATED, _decision(inserted),
                        _notification(notification_row),
                    )
                cur.execute(_SELECT_DECISION,
                            {"decision_id": decision.decision_id})
                decision_row = cur.fetchone()
                cur.execute(_SELECT_NOTIFICATION,
                            {"notification_id": notification.notification_id})
                notification_row = cur.fetchone()
                if decision_row is None or notification_row is None:
                    return CreateDecisionResult(CreateDecisionCode.CONFLICT)
                current_decision = _decision(decision_row)
                current_notification = _notification(notification_row)
                if (current_decision == decision and
                        current_notification == notification):
                    return CreateDecisionResult(
                        CreateDecisionCode.ALREADY_EXISTS,
                        current_decision, current_notification,
                    )
                return CreateDecisionResult(
                    CreateDecisionCode.CONFLICT,
                    current_decision, current_notification,
                )
        except Exception:  # noqa: BLE001 - commit outcome may be ambiguous
            return CreateDecisionResult(CreateDecisionCode.UNKNOWN)

    def read_decision(self, decision_id):
        decision_id = str(UUID(decision_id))
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_SELECT_DECISION, {"decision_id": decision_id})
                row = cur.fetchone()
            if row is None:
                return DecisionReadResult(DecisionReadCode.NOT_FOUND)
            return DecisionReadResult(DecisionReadCode.FOUND, _decision(row))
        except Exception:  # noqa: BLE001 - read boundary fails closed
            return DecisionReadResult(DecisionReadCode.UNAVAILABLE)

    def list_inbox(self, limit=100):
        limit = _positive_limit(limit)
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_LIST_INBOX, {"limit": limit})
                rows = cur.fetchall()
            return InboxResult(
                DecisionReadCode.FOUND, tuple(_decision(row) for row in rows))
        except Exception:  # noqa: BLE001 - read boundary fails closed
            return InboxResult(DecisionReadCode.UNAVAILABLE)

    def compare_and_swap_decision(self, expected, desired):
        self._validate_decision_cas(expected, desired)
        params = _decision_params(desired)
        params["expected_version"] = expected.version
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_CAS_DECISION, params)
                row = cur.fetchone()
                if row is not None:
                    return DecisionCasResult(DecisionCasCode.APPLIED,
                                             _decision(row))
                cur.execute(_SELECT_DECISION,
                            {"decision_id": expected.decision_id})
                current_row = cur.fetchone()
                if current_row is None:
                    return DecisionCasResult(DecisionCasCode.NOT_FOUND)
                return DecisionCasResult(
                    DecisionCasCode.STALE_VERSION, _decision(current_row))
        except Exception:  # noqa: BLE001 - commit outcome may be ambiguous
            return DecisionCasResult(DecisionCasCode.UNKNOWN)

    def claim_notifications(self, owner_token, lease_seconds, limit=100):
        params = {
            "owner_token": _owner(owner_token),
            "lease_seconds": _lease_seconds(lease_seconds),
            "limit": _positive_limit(limit),
        }
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_CLAIM_NOTIFICATIONS, params)
                records = tuple(_notification(row) for row in cur.fetchall())
            if not records:
                return NotificationClaimResult(NotificationClaimCode.EMPTY)
            return NotificationClaimResult(
                NotificationClaimCode.CLAIMED, records)
        except Exception:  # noqa: BLE001 - commit outcome may be ambiguous
            return NotificationClaimResult(NotificationClaimCode.UNKNOWN)

    def compare_and_swap_notification(self, expected, desired):
        self._validate_notification_cas(expected, desired)
        params = _notification_params(desired)
        params.update(
            expected_version=expected.version,
            expected_owner_token=expected.owner_token,
        )
        try:
            with self._connection_factory() as conn, conn.cursor() as cur:
                cur.execute(_CAS_NOTIFICATION, params)
                row = cur.fetchone()
                if row is not None:
                    return NotificationCasResult(
                        NotificationCasCode.APPLIED, _notification(row))
                cur.execute(_SELECT_NOTIFICATION,
                            {"notification_id": expected.notification_id})
                current_row = cur.fetchone()
                if current_row is None:
                    return NotificationCasResult(NotificationCasCode.NOT_FOUND)
                current = _notification(current_row)
                if current.version != expected.version:
                    code = NotificationCasCode.STALE_VERSION
                elif current.owner_token != expected.owner_token:
                    code = NotificationCasCode.OWNER_MISMATCH
                else:
                    code = NotificationCasCode.LEASE_EXPIRED
                return NotificationCasResult(code, current)
        except Exception:  # noqa: BLE001 - commit outcome may be ambiguous
            return NotificationCasResult(NotificationCasCode.UNKNOWN)

    @staticmethod
    def _validate_decision_cas(expected, desired):
        if expected.decision_id != desired.decision_id:
            raise ValueError("CAS decision_id mismatch")
        if desired.version != expected.version + 1:
            raise ValueError("CAS desired version must advance exactly once")
        if not is_legal_decision_transition(expected.status, desired.status):
            raise ValueError("CAS decision status transition is illegal")
        if desired.updated_at < expected.updated_at:
            raise ValueError("CAS decision time cannot move backwards")
        immutable = (
            "event_id", "operation_id", "exchange_position_key", "event_kind",
            "severity", "trigger_context_json", "trigger_context_digest",
            "fallback_action", "fallback_at", "approval_expires_at",
            "created_at", "schema_version",
        )
        if any(getattr(expected, field) != getattr(desired, field)
               for field in immutable):
            raise ValueError("CAS cannot mutate immutable decision identity")

    @staticmethod
    def _validate_notification_cas(expected, desired):
        if expected.notification_id != desired.notification_id:
            raise ValueError("CAS notification_id mismatch")
        if expected.status is not NotificationStatus.CLAIMED:
            raise ValueError("notification CAS requires CLAIMED expected state")
        if desired.version != expected.version + 1:
            raise ValueError("CAS desired version must advance exactly once")
        if desired.updated_at < expected.updated_at:
            raise ValueError("CAS notification time cannot move backwards")
        if desired.status not in {
                NotificationStatus.DELIVERED, NotificationStatus.RETRY_WAIT,
                NotificationStatus.DEAD_LETTER,
                }:
            raise ValueError("notification CAS desired state is illegal")
        immutable = (
            "decision_id", "channel", "dedupe_key", "payload_json",
            "attempt_count", "created_at", "schema_version",
        )
        if any(getattr(expected, field) != getattr(desired, field)
               for field in immutable):
            raise ValueError("CAS cannot mutate immutable notification fields")
