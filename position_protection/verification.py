"""Pure exchange-evidence verification for current-generation protection."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from position_identity.authority import AuthorityStatus, SlotAuthority
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus

ACTIVE_EXCHANGE_PROTECTION_STATUSES = frozenset({"NEW", "WORKING"})
VERIFYABLE_DESIRED_STATUSES = frozenset(
    {
        ProtectionStatus.PENDING,
        ProtectionStatus.SUBMITTING,
        ProtectionStatus.UNKNOWN,
        ProtectionStatus.ACTIVE,
        ProtectionStatus.REPLACING,
    }
)


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains control characters")
    return value


def _positive(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return value


def _timestamp(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value


@dataclass(frozen=True)
class ExchangeExposureObservation:
    exchange_position_key: ExchangePositionKey
    side: str
    quantity: float
    observed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        side = _text(self.side, "side").upper()
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, "observed_at")
        )


@dataclass(frozen=True)
class ExchangeProtectionObservation:
    exchange_position_key: ExchangePositionKey
    algo_alias: str
    status: str
    order_type: str
    reduce_only: bool
    closing_side: str
    trigger_price: float
    quantity: float
    observed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        if not isinstance(self.reduce_only, bool):
            raise TypeError("reduce_only must be bool")
        closing_side = _text(self.closing_side, "closing_side").upper()
        if closing_side not in ("BUY", "SELL"):
            raise ValueError("closing_side must be BUY or SELL")
        object.__setattr__(self, "algo_alias", _text(self.algo_alias, "algo_alias"))
        object.__setattr__(self, "status", _text(self.status, "status").upper())
        object.__setattr__(
            self, "order_type", _text(self.order_type, "order_type").upper()
        )
        object.__setattr__(self, "closing_side", closing_side)
        object.__setattr__(
            self, "trigger_price", _positive(self.trigger_price, "trigger_price")
        )
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, "observed_at")
        )


class ProtectionVerificationCode(str, Enum):
    VERIFIED = "VERIFIED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    AUTHORITY_INACTIVE = "AUTHORITY_INACTIVE"
    DESIRED_NOT_VERIFYABLE = "DESIRED_NOT_VERIFYABLE"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    EXPOSURE_MISMATCH = "EXPOSURE_MISMATCH"
    NO_ALIAS = "NO_ALIAS"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_NOT_ACTIVE = "ORDER_NOT_ACTIVE"
    ORDER_SPEC_MISMATCH = "ORDER_SPEC_MISMATCH"
    AMBIGUOUS_ACTIVE_ORDERS = "AMBIGUOUS_ACTIVE_ORDERS"


@dataclass(frozen=True)
class ProtectionVerificationResult:
    code: ProtectionVerificationCode
    order: ExchangeProtectionObservation | None = None
    message: str = ""

    @property
    def verified(self) -> bool:
        return self.code is ProtectionVerificationCode.VERIFIED


def verify_current_protection(
    *,
    authority: SlotAuthority,
    projection: LivePositionProjection,
    desired: DesiredProtectionRecord,
    exposure: ExchangeExposureObservation,
    orders: tuple[ExchangeProtectionObservation, ...],
    now: float,
    max_evidence_age: float,
    quantity_tolerance: float,
    trigger_tolerance: float,
) -> ProtectionVerificationResult:
    """Prove one exact current-generation protection from bounded evidence."""
    if not isinstance(authority, SlotAuthority):
        raise TypeError("authority must be SlotAuthority")
    if not isinstance(projection, LivePositionProjection):
        raise TypeError("projection must be LivePositionProjection")
    if not isinstance(desired, DesiredProtectionRecord):
        raise TypeError("desired must be DesiredProtectionRecord")
    if not isinstance(exposure, ExchangeExposureObservation):
        raise TypeError("exposure must be ExchangeExposureObservation")
    if not isinstance(orders, tuple) or not all(
        isinstance(order, ExchangeProtectionObservation) for order in orders
    ):
        raise TypeError("orders must be a tuple of ExchangeProtectionObservation")
    now = _timestamp(now, "now")
    max_evidence_age = _nonnegative(max_evidence_age, "max_evidence_age")
    quantity_tolerance = _nonnegative(quantity_tolerance, "quantity_tolerance")
    trigger_tolerance = _nonnegative(trigger_tolerance, "trigger_tolerance")

    key = authority.exchange_position_key
    identity = (
        authority.status is AuthorityStatus.ACTIVE
        and authority.episode_id == projection.episode_id == desired.episode_id
        and authority.slot_generation
        == projection.slot_generation
        == desired.slot_generation
        and projection.exchange_position_key == key
        and desired.exchange_position_key == key
    )
    if authority.status is not AuthorityStatus.ACTIVE:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.AUTHORITY_INACTIVE
        )
    if not identity:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.IDENTITY_MISMATCH
        )
    if desired.status not in VERIFYABLE_DESIRED_STATUSES:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.DESIRED_NOT_VERIFYABLE
        )

    if exposure.exchange_position_key != key or exposure.side != projection.side:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.EXPOSURE_MISMATCH
        )
    if not _close(exposure.quantity, projection.quantity, quantity_tolerance):
        return ProtectionVerificationResult(
            ProtectionVerificationCode.EXPOSURE_MISMATCH
        )
    if not _close(exposure.quantity, desired.covered_quantity, quantity_tolerance):
        return ProtectionVerificationResult(
            ProtectionVerificationCode.EXPOSURE_MISMATCH
        )
    if not _fresh(exposure.observed_at, desired.updated_at, now, max_evidence_age):
        return ProtectionVerificationResult(ProtectionVerificationCode.STALE_EVIDENCE)

    if not desired.exchange_algo_aliases:
        return ProtectionVerificationResult(ProtectionVerificationCode.NO_ALIAS)
    aliased = tuple(
        order for order in orders if order.algo_alias in desired.exchange_algo_aliases
    )
    if not aliased:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.ORDER_NOT_FOUND
        )
    active = tuple(
        order
        for order in aliased
        if order.status in ACTIVE_EXCHANGE_PROTECTION_STATUSES
    )
    if not active:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.ORDER_NOT_ACTIVE, aliased[0]
        )
    if len(active) != 1:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.AMBIGUOUS_ACTIVE_ORDERS
        )
    order = active[0]
    if not _fresh(order.observed_at, desired.updated_at, now, max_evidence_age):
        return ProtectionVerificationResult(
            ProtectionVerificationCode.STALE_EVIDENCE, order
        )
    expected_side = "SELL" if projection.side == "LONG" else "BUY"
    spec_matches = (
        order.exchange_position_key == key
        and order.order_type == "STOP_MARKET"
        and order.reduce_only
        and order.closing_side == expected_side == desired.closing_side
        and _close(order.trigger_price, desired.trigger_price, trigger_tolerance)
        and _close(order.quantity, desired.covered_quantity, quantity_tolerance)
    )
    if not spec_matches:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.ORDER_SPEC_MISMATCH, order
        )

    foreign_active = tuple(
        candidate
        for candidate in orders
        if candidate is not order
        and candidate.exchange_position_key == key
        and candidate.order_type == "STOP_MARKET"
        and candidate.status in ACTIVE_EXCHANGE_PROTECTION_STATUSES
    )
    if foreign_active:
        return ProtectionVerificationResult(
            ProtectionVerificationCode.AMBIGUOUS_ACTIVE_ORDERS,
            order,
            message="another active STOP_MARKET order exists for the slot",
        )
    return ProtectionVerificationResult(ProtectionVerificationCode.VERIFIED, order)


def _nonnegative(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(left - right) <= tolerance


def _fresh(observed_at: float, required_after: float, now: float, max_age: float) -> bool:
    return required_after <= observed_at <= now and now - observed_at <= max_age
