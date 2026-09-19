"""Atomic canonical-token commit of verified protection ACTIVE state."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from position_identity.authority import SlotAuthority
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord, ProtectionStatus
from position_protection.verification import (
    ExchangeExposureObservation,
    ExchangeProtectionObservation,
    ProtectionVerificationResult,
    verify_current_protection,
)

VERIFIED_ACTIVE_COMMIT_LUA = r"""
local authority = redis.call('GET', KEYS[1])
local projection = redis.call('GET', KEYS[2])
local desired = redis.call('GET', KEYS[3])
if authority ~= ARGV[1] or projection ~= ARGV[2] or desired ~= ARGV[3] then
  return {'STALE', desired or ''}
end
if desired == ARGV[4] then return {'ALREADY_ACTIVE', desired} end
redis.call('SET', KEYS[3], ARGV[4])
return {'APPLIED', ARGV[4]}
"""


class VerifiedActiveCommitCode(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    NOT_VERIFIED = "NOT_VERIFIED"
    STALE = "STALE"
    NOT_FOUND = "NOT_FOUND"
    MALFORMED = "MALFORMED"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class VerifiedActiveCommitResult:
    code: VerifiedActiveCommitCode
    desired: DesiredProtectionRecord | None = None
    verification: ProtectionVerificationResult | None = None
    message: str = ""

    @property
    def applied(self) -> bool:
        return self.code in (
            VerifiedActiveCommitCode.APPLIED,
            VerifiedActiveCommitCode.ALREADY_ACTIVE,
        )


class RedisVerifiedActiveCommitAdapter:
    """Verify evidence, then CAS ACTIVE against all three canonical tokens."""

    def __init__(
        self,
        *,
        redis_get: Callable,
        redis_eval: Callable,
        redis_available: Callable[[], bool] | None = None,
    ) -> None:
        self._redis_get = redis_get
        self._redis_eval = redis_eval
        self._redis_available = redis_available

    def commit(
        self,
        *,
        exchange_position_key: ExchangePositionKey,
        exposure: ExchangeExposureObservation,
        orders: tuple[ExchangeProtectionObservation, ...],
        operation_id: str,
        now: float,
        max_evidence_age: float,
        quantity_tolerance: float,
        trigger_tolerance: float,
    ) -> VerifiedActiveCommitResult:
        if not isinstance(exchange_position_key, ExchangePositionKey):
            raise TypeError("exchange_position_key must be ExchangePositionKey")
        operation_id = self._text(operation_id, "operation_id")
        now = self._time(now, "now")
        unavailable = self._availability()
        if unavailable:
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.UNAVAILABLE, message=unavailable
            )
        digest = exchange_position_key.canonical_digest()
        keys = (
            exchange_position_key.to_storage_key(),
            f"pm:position-projection:v1:{digest}",
            f"pm:desired-protection:v1:{digest}",
        )
        try:
            raw_authority, raw_projection, raw_desired = (
                self._redis_get(key) for key in keys
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.UNAVAILABLE, message=str(exc)
            )
        if raw_authority is None or raw_projection is None or raw_desired is None:
            return VerifiedActiveCommitResult(VerifiedActiveCommitCode.NOT_FOUND)
        try:
            authority = SlotAuthority.from_json(raw_authority)
            projection = LivePositionProjection.from_json(raw_projection)
            desired = DesiredProtectionRecord.from_json(raw_desired)
        except (TypeError, ValueError, UnicodeError) as exc:
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.MALFORMED, message=str(exc)
            )
        if (
            authority.exchange_position_key != exchange_position_key
            or projection.exchange_position_key != exchange_position_key
            or desired.exchange_position_key != exchange_position_key
        ):
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.MALFORMED,
                message="canonical record slot does not match storage key",
            )

        verification = None
        same_operation_recovery = (
            desired.status is ProtectionStatus.ACTIVE
            and desired.last_operation_id == operation_id
        )
        if same_operation_recovery:
            proposed = desired
        else:
            try:
                verification = verify_current_protection(
                    authority=authority,
                    projection=projection,
                    desired=desired,
                    exposure=exposure,
                    orders=orders,
                    now=now,
                    max_evidence_age=max_evidence_age,
                    quantity_tolerance=quantity_tolerance,
                    trigger_tolerance=trigger_tolerance,
                )
            except (TypeError, ValueError) as exc:
                return VerifiedActiveCommitResult(
                    VerifiedActiveCommitCode.INVALID,
                    desired=desired,
                    message=str(exc),
                )
            if not verification.verified:
                return VerifiedActiveCommitResult(
                    VerifiedActiveCommitCode.NOT_VERIFIED,
                    desired=desired,
                    verification=verification,
                )
            if desired.status is ProtectionStatus.ACTIVE:
                proposed = desired
            else:
                try:
                    proposed = desired.transition(
                        status=ProtectionStatus.ACTIVE,
                        operation_id=operation_id,
                        now=now,
                    )
                except (TypeError, ValueError) as exc:
                    return VerifiedActiveCommitResult(
                        VerifiedActiveCommitCode.INVALID,
                        desired=desired,
                        verification=verification,
                        message=str(exc),
                    )
        try:
            response = self._redis_eval(
                VERIFIED_ACTIVE_COMMIT_LUA,
                3,
                *keys,
                self._raw_text(raw_authority),
                self._raw_text(raw_projection),
                self._raw_text(raw_desired),
                proposed.to_json(),
            )
        except Exception as exc:  # noqa: BLE001 - ACK may be ambiguous
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.UNKNOWN,
                desired=proposed,
                verification=verification,
                message=str(exc),
            )
        return self._parse(response, verification)

    def _availability(self) -> str:
        if self._redis_available is None:
            return ""
        try:
            return "" if self._redis_available() is True else "Redis unavailable"
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    @staticmethod
    def _parse(response, verification) -> VerifiedActiveCommitResult:
        if not isinstance(response, (list, tuple)) or len(response) != 2:
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.UNKNOWN,
                verification=verification,
                message="invalid verified-active response",
            )
        raw_code, raw_desired = response
        if isinstance(raw_code, bytes):
            try:
                raw_code = raw_code.decode("utf-8")
            except UnicodeError:
                return VerifiedActiveCommitResult(
                    VerifiedActiveCommitCode.UNKNOWN,
                    verification=verification,
                    message="verified-active response code is not UTF-8",
                )
        try:
            code = VerifiedActiveCommitCode(raw_code)
        except (TypeError, ValueError):
            return VerifiedActiveCommitResult(
                VerifiedActiveCommitCode.UNKNOWN,
                verification=verification,
                message=f"unknown verified-active response: {raw_code!r}",
            )
        desired = None
        if raw_desired:
            try:
                desired = DesiredProtectionRecord.from_json(raw_desired)
            except (TypeError, ValueError, UnicodeError) as exc:
                return VerifiedActiveCommitResult(
                    VerifiedActiveCommitCode.MALFORMED,
                    verification=verification,
                    message=str(exc),
                )
        return VerifiedActiveCommitResult(code, desired, verification)

    @staticmethod
    def _raw_text(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if not isinstance(value, str):
            raise TypeError("Redis canonical state must be text")
        return value

    @staticmethod
    def _text(value, field) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required")
        return value.strip()

    @staticmethod
    def _time(value, field) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"{field} must be finite and nonnegative")
        return float(value)
