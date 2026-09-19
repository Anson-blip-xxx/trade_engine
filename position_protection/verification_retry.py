"""Bounded caller policy for repeated exchange-protection verification."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from position_protection.verification import (
    ProtectionVerificationCode,
    ProtectionVerificationResult,
)


class VerificationRetryAction(str, Enum):
    COMMIT_VERIFIED = "COMMIT_VERIFIED"
    QUERY_AGAIN = "QUERY_AGAIN"
    EXHAUSTED = "EXHAUSTED"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True)
class VerificationRetryPolicy:
    max_attempts: int
    deadline_seconds: float
    base_delay_seconds: float
    max_delay_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for field in ("deadline_seconds", "base_delay_seconds", "max_delay_seconds"):
            value = _nonnegative(getattr(self, field), field)
            object.__setattr__(self, field, value)
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")


@dataclass(frozen=True)
class VerificationRetryDecision:
    action: VerificationRetryAction
    retry_at: float | None = None


_RETRYABLE = frozenset(
    {
        ProtectionVerificationCode.STALE_EVIDENCE,
        ProtectionVerificationCode.ORDER_NOT_FOUND,
        ProtectionVerificationCode.ORDER_NOT_ACTIVE,
    }
)


def decide_verification_retry(
    *,
    result: ProtectionVerificationResult,
    attempts_completed: int,
    started_at: float,
    now: float,
    policy: VerificationRetryPolicy,
) -> VerificationRetryDecision:
    if not isinstance(result, ProtectionVerificationResult):
        raise TypeError("result must be ProtectionVerificationResult")
    if isinstance(attempts_completed, bool) or not isinstance(attempts_completed, int) or attempts_completed < 1:
        raise ValueError("attempts_completed must be a positive integer")
    if not isinstance(policy, VerificationRetryPolicy):
        raise TypeError("policy must be VerificationRetryPolicy")
    started_at = _nonnegative(started_at, "started_at")
    now = _nonnegative(now, "now")
    if now < started_at:
        raise ValueError("now must be >= started_at")
    if result.verified:
        return VerificationRetryDecision(VerificationRetryAction.COMMIT_VERIFIED)
    if result.code not in _RETRYABLE:
        return VerificationRetryDecision(VerificationRetryAction.QUARANTINE)
    deadline = started_at + policy.deadline_seconds
    if attempts_completed >= policy.max_attempts or now >= deadline:
        return VerificationRetryDecision(VerificationRetryAction.EXHAUSTED)
    delay = min(
        policy.base_delay_seconds * (2 ** (attempts_completed - 1)),
        policy.max_delay_seconds,
    )
    retry_at = now + delay
    if retry_at > deadline:
        return VerificationRetryDecision(VerificationRetryAction.EXHAUSTED)
    return VerificationRetryDecision(VerificationRetryAction.QUERY_AGAIN, retry_at)


def _nonnegative(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return value
