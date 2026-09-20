"""Backend-neutral records for the operator inbox and notification outbox."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from enum import Enum
from uuid import UUID

from operation_journal.delayed_policy import DelayedAction, DelayedEventKind
from operation_journal.model import canonical_json
from position_identity.slot import ExchangePositionKey

DECISION_SCHEMA_VERSION = 1
NOTIFICATION_SCHEMA_VERSION = 1


class DecisionSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DecisionStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class DecisionResolution(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    QUARANTINE = "QUARANTINE"
    REBUILD = "REBUILD"
    AUTOMATIC_FALLBACK = "AUTOMATIC_FALLBACK"


_DECISION_TRANSITIONS = {
    DecisionStatus.OPEN: {
        DecisionStatus.ACKNOWLEDGED, DecisionStatus.RESOLVED,
        DecisionStatus.EXPIRED, DecisionStatus.SUPERSEDED,
    },
    DecisionStatus.ACKNOWLEDGED: {
        DecisionStatus.RESOLVED, DecisionStatus.EXPIRED,
        DecisionStatus.SUPERSEDED,
    },
    DecisionStatus.RESOLVED: set(),
    DecisionStatus.EXPIRED: set(),
    DecisionStatus.SUPERSEDED: set(),
}


def is_legal_decision_transition(current, desired):
    if not isinstance(current, DecisionStatus) or not isinstance(
            desired, DecisionStatus):
        return False
    if current is desired:
        return current in {DecisionStatus.OPEN, DecisionStatus.ACKNOWLEDGED}
    return desired in _DECISION_TRANSITIONS[current]


@dataclass(frozen=True)
class DecisionItem:
    decision_id: str
    event_id: str
    exchange_position_key: ExchangePositionKey
    event_kind: DelayedEventKind
    severity: DecisionSeverity
    status: DecisionStatus
    version: int
    trigger_context_json: str
    current_context_json: str
    trigger_context_digest: str
    current_context_digest: str
    fallback_action: DelayedAction
    fallback_at: float
    created_at: float
    updated_at: float
    operation_id: str | None = None
    approval_expires_at: float | None = None
    assigned_to: str | None = None
    resolution: DecisionResolution | None = None
    resolution_json: str = "{}"
    schema_version: int = DECISION_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != DECISION_SCHEMA_VERSION:
            raise ValueError("unsupported decision schema version")
        for field in ("decision_id", "event_id"):
            object.__setattr__(self, field, str(UUID(getattr(self, field))))
        if self.operation_id is not None:
            object.__setattr__(self, "operation_id", str(UUID(self.operation_id)))
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        for field, expected in (
                ("event_kind", DelayedEventKind),
                ("severity", DecisionSeverity),
                ("status", DecisionStatus),
                ("fallback_action", DelayedAction),
                ):
            if not isinstance(getattr(self, field), expected):
                raise TypeError(f"{field} must be {expected.__name__}")
        _positive_int(self.version, "version")
        for field in ("trigger_context_json", "current_context_json",
                      "resolution_json"):
            value = _object_json(getattr(self, field), field)
            object.__setattr__(self, field, value)
        for field in ("trigger_context_digest", "current_context_digest"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        fallback = _time(self.fallback_at, "fallback_at")
        created = _time(self.created_at, "created_at")
        updated = _time(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at must be >= created_at")
        if fallback < created:
            raise ValueError("fallback_at must be >= created_at")
        object.__setattr__(self, "fallback_at", fallback)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        if self.approval_expires_at is not None:
            expires = _time(self.approval_expires_at, "approval_expires_at")
            if expires <= created:
                raise ValueError("approval_expires_at must be greater than created_at")
            object.__setattr__(self, "approval_expires_at", expires)
        object.__setattr__(self, "assigned_to", _text(
            self.assigned_to, "assigned_to", optional=True))
        if self.resolution is not None and not isinstance(
                self.resolution, DecisionResolution):
            raise TypeError("resolution must be DecisionResolution or None")
        if self.status is DecisionStatus.RESOLVED and self.resolution is None:
            raise ValueError("RESOLVED decision requires resolution")
        if self.status is not DecisionStatus.RESOLVED and self.resolution is not None:
            raise ValueError("only RESOLVED decision may carry resolution")
        if (self.status is not DecisionStatus.RESOLVED and
                self.resolution_json != "{}"):
            raise ValueError("only RESOLVED decision may carry resolution detail")

    @classmethod
    def open(
            cls, *, decision_id, event_id, exchange_position_key, event_kind,
            severity, trigger_context, current_context, trigger_context_digest,
            current_context_digest, fallback_action, fallback_at, now,
            operation_id=None, approval_expires_at=None,
            ):
        return cls(
            decision_id=decision_id, event_id=event_id,
            exchange_position_key=exchange_position_key,
            event_kind=event_kind, severity=severity, status=DecisionStatus.OPEN,
            version=1,
            trigger_context_json=canonical_json(
                trigger_context, "trigger_context"),
            current_context_json=canonical_json(
                current_context, "current_context"),
            trigger_context_digest=trigger_context_digest,
            current_context_digest=current_context_digest,
            fallback_action=fallback_action, fallback_at=fallback_at,
            created_at=now, updated_at=now, operation_id=operation_id,
            approval_expires_at=approval_expires_at,
        )

    def refresh_current(self, *, current_context, current_context_digest, now):
        if self.status not in {DecisionStatus.OPEN, DecisionStatus.ACKNOWLEDGED}:
            raise ValueError("terminal decision cannot refresh current context")
        return replace(
            self,
            current_context_json=canonical_json(
                current_context, "current_context"),
            current_context_digest=current_context_digest,
            version=self.version + 1,
            updated_at=_forward_time(now, self.updated_at),
        )

    def transition(
            self, *, status, now, assigned_to=None, resolution=None,
            resolution_detail=None,
            ):
        if not isinstance(status, DecisionStatus):
            raise TypeError("status must be DecisionStatus")
        if (status is self.status or
                not is_legal_decision_transition(self.status, status)):
            raise ValueError(
                f"illegal decision transition {self.status.value}->{status.value}")
        detail = {} if resolution_detail is None else resolution_detail
        return replace(
            self,
            status=status,
            version=self.version + 1,
            updated_at=_forward_time(now, self.updated_at),
            assigned_to=(self.assigned_to if assigned_to is None else assigned_to),
            resolution=resolution,
            resolution_json=canonical_json(detail, "resolution_detail"),
        )


class NotificationChannel(str, Enum):
    TELEGRAM = "TELEGRAM"


class NotificationStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRY_WAIT = "RETRY_WAIT"
    DELIVERED = "DELIVERED"
    DEAD_LETTER = "DEAD_LETTER"


@dataclass(frozen=True)
class NotificationOutboxItem:
    notification_id: str
    decision_id: str
    channel: NotificationChannel
    dedupe_key: str
    status: NotificationStatus
    version: int
    payload_json: str
    attempt_count: int
    next_attempt_at: float
    created_at: float
    updated_at: float
    owner_token: str | None = None
    lease_expires_at: float | None = None
    delivered_at: float | None = None
    last_error: str | None = None
    schema_version: int = NOTIFICATION_SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != NOTIFICATION_SCHEMA_VERSION:
            raise ValueError("unsupported notification schema version")
        for field in ("notification_id", "decision_id"):
            object.__setattr__(self, field, str(UUID(getattr(self, field))))
        if not isinstance(self.channel, NotificationChannel):
            raise TypeError("channel must be NotificationChannel")
        if not isinstance(self.status, NotificationStatus):
            raise TypeError("status must be NotificationStatus")
        object.__setattr__(self, "dedupe_key", _text(
            self.dedupe_key, "dedupe_key"))
        _positive_int(self.version, "version")
        if (isinstance(self.attempt_count, bool) or
                not isinstance(self.attempt_count, int) or
                self.attempt_count < 0):
            raise ValueError("attempt_count must be a nonnegative integer")
        object.__setattr__(self, "payload_json", _object_json(
            self.payload_json, "payload_json"))
        for field in ("next_attempt_at", "created_at", "updated_at"):
            object.__setattr__(self, field, _time(getattr(self, field), field))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be >= created_at")
        if self.next_attempt_at < self.created_at:
            raise ValueError("next_attempt_at must be >= created_at")
        object.__setattr__(self, "owner_token", _text(
            self.owner_token, "owner_token", optional=True))
        object.__setattr__(self, "last_error", _text(
            self.last_error, "last_error", optional=True))
        for field in ("lease_expires_at", "delivered_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _time(value, field))
        if self.status is NotificationStatus.CLAIMED:
            if self.owner_token is None or self.lease_expires_at is None:
                raise ValueError("CLAIMED notification requires owner lease")
            if self.lease_expires_at <= self.updated_at:
                raise ValueError("CLAIMED notification requires a live lease")
        elif self.owner_token is not None or self.lease_expires_at is not None:
            raise ValueError("only CLAIMED notification may carry owner lease")
        if self.status is NotificationStatus.DELIVERED:
            if self.delivered_at is None:
                raise ValueError("DELIVERED notification requires delivered_at")
        elif self.delivered_at is not None:
            raise ValueError("only DELIVERED notification may carry delivered_at")
        if (self.delivered_at is not None and
                not self.created_at <= self.delivered_at <= self.updated_at):
            raise ValueError("delivered_at must be within record lifetime")
        if self.status is NotificationStatus.PENDING and self.attempt_count != 0:
            raise ValueError("PENDING notification cannot have attempts")
        if (self.status is not NotificationStatus.PENDING and
                self.attempt_count < 1):
            raise ValueError("non-PENDING notification requires an attempt")
        if (self.status is NotificationStatus.RETRY_WAIT and
                (self.last_error is None or
                 self.next_attempt_at <= self.updated_at)):
            raise ValueError(
                "RETRY_WAIT requires an error and future next_attempt_at")
        if (self.status is NotificationStatus.DEAD_LETTER and
                self.last_error is None):
            raise ValueError("DEAD_LETTER requires last_error")

    @classmethod
    def pending(
            cls, *, notification_id, decision_id, dedupe_key, payload, now,
            channel=NotificationChannel.TELEGRAM,
            ):
        return cls(
            notification_id=notification_id, decision_id=decision_id,
            channel=channel, dedupe_key=dedupe_key,
            status=NotificationStatus.PENDING, version=1,
            payload_json=canonical_json(payload, "payload"), attempt_count=0,
            next_attempt_at=now, created_at=now, updated_at=now,
        )

    def claim(self, *, owner_token, lease_expires_at, now):
        claimable = {
            NotificationStatus.PENDING, NotificationStatus.RETRY_WAIT,
            NotificationStatus.CLAIMED,
        }
        if self.status not in claimable:
            raise ValueError(
                "only pending/retry/expired-claim notification can be claimed")
        now = _forward_time(now, self.updated_at)
        if (self.status is NotificationStatus.CLAIMED and
                now < self.lease_expires_at):
            raise ValueError("notification claim lease is still active")
        if now < self.next_attempt_at:
            raise ValueError("notification is not due")
        lease = _time(lease_expires_at, "lease_expires_at")
        if lease <= now:
            raise ValueError("lease_expires_at must be greater than now")
        return replace(
            self, status=NotificationStatus.CLAIMED,
            version=self.version + 1, attempt_count=self.attempt_count + 1,
            owner_token=owner_token, lease_expires_at=lease, updated_at=now,
        )

    def delivered(self, *, now):
        self._require_claimed()
        now = _forward_time(now, self.updated_at)
        return replace(
            self, status=NotificationStatus.DELIVERED,
            version=self.version + 1, owner_token=None, lease_expires_at=None,
            delivered_at=now, updated_at=now,
        )

    def retry(self, *, next_attempt_at, last_error, now):
        self._require_claimed()
        now = _forward_time(now, self.updated_at)
        retry_at = _time(next_attempt_at, "next_attempt_at")
        if retry_at <= now:
            raise ValueError("next_attempt_at must be greater than now")
        return replace(
            self, status=NotificationStatus.RETRY_WAIT,
            version=self.version + 1, owner_token=None, lease_expires_at=None,
            next_attempt_at=retry_at, last_error=last_error, updated_at=now,
        )

    def dead_letter(self, *, last_error, now):
        self._require_claimed()
        now = _forward_time(now, self.updated_at)
        return replace(
            self, status=NotificationStatus.DEAD_LETTER,
            version=self.version + 1, owner_token=None, lease_expires_at=None,
            last_error=last_error, updated_at=now,
        )

    def _require_claimed(self):
        if self.status is not NotificationStatus.CLAIMED:
            raise ValueError("notification must be CLAIMED")


def _object_json(value, field):
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise TypeError(f"{field} must contain a JSON object")
    return canonical_json(parsed, field)


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _time(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return round(value, 6)


def _forward_time(value, previous):
    value = _time(value, "now")
    if value < previous:
        raise ValueError("time cannot move backwards")
    return value


def _digest(value, field):
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    value = value.strip()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _text(value, field, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value
