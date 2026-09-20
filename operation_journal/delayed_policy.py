"""Pure policy for delayed recovery events and expiring operator approvals."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from operation_journal.model import OperationType
from position_identity.slot import ExchangePositionKey


class DelayedEventKind(str, Enum):
    RISK_INCREASING_REQUEST = "RISK_INCREASING_REQUEST"
    UNKNOWN_MUTATION = "UNKNOWN_MUTATION"
    UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
    EXTERNAL_POSITION = "EXTERNAL_POSITION"
    OPERATOR_APPROVAL = "OPERATOR_APPROVAL"


class DelayedAction(str, Enum):
    REVALIDATE_REQUEST = "REVALIDATE_REQUEST"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    QUERY_AND_RECONCILE = "QUERY_AND_RECONCILE"
    QUARANTINE_AND_QUERY = "QUARANTINE_AND_QUERY"
    RESTORE_PROTECTION = "RESTORE_PROTECTION"
    PAUSE_OPENS_AND_RESTORE_PROTECTION = "PAUSE_OPENS_AND_RESTORE_PROTECTION"
    QUARANTINE_AND_RESTORE_PROTECTION = "QUARANTINE_AND_RESTORE_PROTECTION"
    EMERGENCY_REDUCE_ONLY = "EMERGENCY_REDUCE_ONLY"
    WAIT_FOR_APPROVAL = "WAIT_FOR_APPROVAL"
    REBUILD_DECISION = "REBUILD_DECISION"


@dataclass(frozen=True)
class DelayedEvent:
    event_id: str
    kind: DelayedEventKind
    occurred_at: float
    context_digest: str

    def __post_init__(self):
        object.__setattr__(self, "event_id", str(UUID(self.event_id)))
        if not isinstance(self.kind, DelayedEventKind):
            raise TypeError("kind must be DelayedEventKind")
        object.__setattr__(self, "occurred_at", _time(
            self.occurred_at, "occurred_at"))
        object.__setattr__(self, "context_digest", _digest(
            self.context_digest, "context_digest"))


@dataclass(frozen=True)
class AutonomySafetyPolicy:
    short_retry_seconds: float
    isolation_seconds: float
    emergency_seconds: float
    approval_wait_seconds: float
    automatic_emergency_close: bool = False

    def __post_init__(self):
        values = []
        for field in (
                "short_retry_seconds", "isolation_seconds",
                "emergency_seconds", "approval_wait_seconds",
                ):
            value = _time(getattr(self, field), field, positive=True)
            object.__setattr__(self, field, value)
            values.append(value)
        if not values[0] < values[1] < values[2]:
            raise ValueError(
                "short_retry_seconds < isolation_seconds < emergency_seconds required")
        if not isinstance(self.automatic_emergency_close, bool):
            raise TypeError("automatic_emergency_close must be boolean")


@dataclass(frozen=True)
class DelayedDecision:
    action: DelayedAction
    age_seconds: float
    exchange_mutation_candidate: bool = False
    requires_generation_revalidation: bool = False
    requires_mutation_ownership: bool = False
    requires_new_event: bool = False
    reason: str = ""

    @property
    def may_increase_risk(self) -> bool:
        return False


def decide_delayed_event(
        event: DelayedEvent,
        *,
        now: float,
        policy: AutonomySafetyPolicy,
        current_context_digest: str,
        ) -> DelayedDecision:
    """Choose a fallback without performing I/O or authorizing side effects."""
    if not isinstance(event, DelayedEvent):
        raise TypeError("event must be DelayedEvent")
    if not isinstance(policy, AutonomySafetyPolicy):
        raise TypeError("policy must be AutonomySafetyPolicy")
    current_digest = _digest(current_context_digest, "current_context_digest")
    current = _time(now, "now")
    if current < event.occurred_at:
        raise ValueError("now cannot precede occurred_at")
    age = round(current - event.occurred_at, 6)
    if event.context_digest != current_digest:
        return DelayedDecision(
            DelayedAction.REBUILD_DECISION,
            age,
            requires_new_event=True,
            reason="the original context is stale; old work cannot be rebound",
        )
    if event.kind is DelayedEventKind.RISK_INCREASING_REQUEST:
        if age <= policy.short_retry_seconds:
            return DelayedDecision(
                DelayedAction.REVALIDATE_REQUEST,
                age,
                requires_generation_revalidation=True,
                reason="fresh risk request still requires current admission",
            )
        return DelayedDecision(
            DelayedAction.CANCEL_REQUEST,
            age,
            requires_new_event=True,
            reason="expired risk-increasing work is never replayed",
        )
    if event.kind is DelayedEventKind.UNKNOWN_MUTATION:
        action = DelayedAction.QUERY_AND_RECONCILE
        if age > policy.isolation_seconds:
            action = DelayedAction.QUARANTINE_AND_QUERY
        return DelayedDecision(
            action,
            age,
            requires_generation_revalidation=True,
            reason="UNKNOWN may have committed and cannot be blindly retried",
        )
    if event.kind is DelayedEventKind.EXTERNAL_POSITION:
        return DelayedDecision(
            DelayedAction.QUARANTINE_AND_QUERY,
            age,
            requires_generation_revalidation=True,
            reason="external exposure cannot inherit native lineage",
        )
    if event.kind is DelayedEventKind.OPERATOR_APPROVAL:
        if age <= policy.approval_wait_seconds:
            return DelayedDecision(
                DelayedAction.WAIT_FOR_APPROVAL,
                age,
                reason="approval window is still open",
            )
        return DelayedDecision(
            DelayedAction.REBUILD_DECISION,
            age,
            requires_new_event=True,
            reason="approval window expired; current evidence is required",
        )
    return _unprotected_decision(age, policy)


def _unprotected_decision(age, policy):
    if age <= policy.short_retry_seconds:
        action = DelayedAction.RESTORE_PROTECTION
    elif age <= policy.isolation_seconds:
        action = DelayedAction.PAUSE_OPENS_AND_RESTORE_PROTECTION
    elif age <= policy.emergency_seconds:
        action = DelayedAction.QUARANTINE_AND_RESTORE_PROTECTION
    elif policy.automatic_emergency_close:
        action = DelayedAction.EMERGENCY_REDUCE_ONLY
    else:
        action = DelayedAction.QUARANTINE_AND_RESTORE_PROTECTION
    return DelayedDecision(
        action,
        age,
        exchange_mutation_candidate=True,
        requires_generation_revalidation=True,
        requires_mutation_ownership=True,
        reason="risk reduction still requires current identity and ownership",
    )


class OperatorAction(str, Enum):
    RETRY_MUTATION = "RETRY_MUTATION"
    ADOPT_EXTERNAL = "ADOPT_EXTERNAL"
    RESUME_NEW_OPENS = "RESUME_NEW_OPENS"
    EMERGENCY_REDUCE_ONLY = "EMERGENCY_REDUCE_ONLY"
    APPLY_COMPENSATION = "APPLY_COMPENSATION"


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class ApprovalBinding:
    operation_id: str
    operation_type: OperationType
    operation_version: int
    exchange_position_key: ExchangePositionKey
    episode_id: str
    lifecycle_generation: int
    protection_generation: int | None
    authority_revision: int
    desired_revision: int | None
    policy_version: str
    evidence_observed_at: float
    position_direction: PositionDirection
    position_quantity: str
    evidence_digest: str
    action: OperatorAction

    def __post_init__(self):
        for field in ("operation_id", "episode_id"):
            object.__setattr__(self, field, str(UUID(getattr(self, field))))
        if not isinstance(self.operation_type, OperationType):
            raise TypeError("operation_type must be OperationType")
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        for field in (
                "operation_version", "lifecycle_generation",
                "authority_revision",
                ):
            _positive_int(getattr(self, field), field)
        for field in ("protection_generation", "desired_revision"):
            value = getattr(self, field)
            if value is not None:
                _positive_int(value, field)
        object.__setattr__(self, "policy_version", _text(
            self.policy_version, "policy_version"))
        object.__setattr__(self, "evidence_observed_at", _time(
            self.evidence_observed_at, "evidence_observed_at"))
        if not isinstance(self.position_direction, PositionDirection):
            raise TypeError("position_direction must be PositionDirection")
        quantity = canonical_quantity(self.position_quantity)
        if self.position_direction is PositionDirection.FLAT:
            if Decimal(quantity) != 0:
                raise ValueError("FLAT position must have zero quantity")
        elif Decimal(quantity) <= 0:
            raise ValueError("LONG/SHORT position must have positive quantity")
        object.__setattr__(self, "position_quantity", quantity)
        object.__setattr__(self, "evidence_digest", _digest(
            self.evidence_digest, "evidence_digest"))
        if not isinstance(self.action, OperatorAction):
            raise TypeError("action must be OperatorAction")


@dataclass(frozen=True)
class OperatorApproval:
    approval_id: str
    binding: ApprovalBinding
    issued_at: float
    expires_at: float

    def __post_init__(self):
        object.__setattr__(self, "approval_id", str(UUID(self.approval_id)))
        if not isinstance(self.binding, ApprovalBinding):
            raise TypeError("binding must be ApprovalBinding")
        issued = _time(self.issued_at, "issued_at")
        expires = _time(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be greater than issued_at")
        if self.binding.evidence_observed_at > issued:
            raise ValueError("approval cannot precede its exchange evidence")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)


class ApprovalCode(str, Enum):
    VALID = "VALID"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"


def validate_operator_approval(
        approval: OperatorApproval,
        *,
        expected: ApprovalBinding,
        now: float,
        ) -> ApprovalCode:
    if not isinstance(approval, OperatorApproval):
        raise TypeError("approval must be OperatorApproval")
    if not isinstance(expected, ApprovalBinding):
        raise TypeError("expected must be ApprovalBinding")
    current = _time(now, "now")
    if approval.binding != expected:
        return ApprovalCode.CONTEXT_MISMATCH
    if current < approval.issued_at:
        return ApprovalCode.NOT_YET_VALID
    if current >= approval.expires_at:
        return ApprovalCode.EXPIRED
    return ApprovalCode.VALID


def canonical_quantity(value) -> str:
    """Normalize a quantity before including it in an evidence digest."""
    if isinstance(value, bool):
        raise TypeError("quantity must be decimal-compatible")
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("quantity must be decimal-compatible") from exc
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("quantity must be finite and nonnegative")
    normalized = format(quantity.normalize(), "f")
    return "0" if Decimal(normalized) == 0 else normalized


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _time(value, field, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field} must be finite and {qualifier}")
    return round(value, 6)


def _digest(value, field):
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    value = value.strip()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value
