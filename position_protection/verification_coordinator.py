"""One-step, non-blocking orchestration of protection verification."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from position_identity.slot import ExchangePositionKey
from position_protection.binance_observation import (
    BinanceObservationCode,
    BinanceObservationResult,
    normalize_binance_verification_snapshot,
)
from position_protection.verification_commit import (
    VerifiedActiveCommitCode,
    VerifiedActiveCommitResult,
)
from position_protection.verification_retry import (
    VerificationRetryAction,
    VerificationRetryPolicy,
    decide_verification_retry,
)


@dataclass(frozen=True)
class BinanceVerificationSnapshot:
    position_risk_payload: object
    open_algo_orders_payload: object
    observed_at: float


class BinanceVerificationQueryPort(Protocol):
    def fetch(self, key: ExchangePositionKey) -> BinanceVerificationSnapshot: ...


class VerifiedActiveCommitPort(Protocol):
    def commit(self, **kwargs) -> VerifiedActiveCommitResult: ...


@dataclass(frozen=True)
class VerificationSession:
    operation_id: str
    started_at: float
    attempts_completed: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id is required")
        if isinstance(self.started_at, bool) or not isinstance(
            self.started_at, (int, float)
        ) or self.started_at < 0:
            raise ValueError("started_at must be nonnegative")
        if isinstance(self.attempts_completed, bool) or not isinstance(
            self.attempts_completed, int
        ) or self.attempts_completed < 0:
            raise ValueError("attempts_completed must be a nonnegative integer")


class VerificationCoordinatorCode(str, Enum):
    ACTIVE = "ACTIVE"
    QUERY_AGAIN = "QUERY_AGAIN"
    EXHAUSTED = "EXHAUSTED"
    QUARANTINE = "QUARANTINE"
    RELOAD_CANONICAL = "RELOAD_CANONICAL"
    RESOLVE_UNKNOWN = "RESOLVE_UNKNOWN"


@dataclass(frozen=True)
class VerificationCoordinatorResult:
    code: VerificationCoordinatorCode
    session: VerificationSession
    retry_at: float | None = None
    observation: BinanceObservationResult | None = None
    commit: VerifiedActiveCommitResult | None = None
    message: str = ""


class ProtectionVerificationCoordinator:
    """Perform exactly one query/normalize/commit attempt; never loop or sleep."""

    def __init__(self, *, query_port, commit_port, retry_policy) -> None:
        if not isinstance(retry_policy, VerificationRetryPolicy):
            raise TypeError("retry_policy must be VerificationRetryPolicy")
        self._query = query_port
        self._commit = commit_port
        self._policy = retry_policy

    def step(
        self,
        *,
        session: VerificationSession,
        exchange_position_key: ExchangePositionKey,
        now: float,
        max_evidence_age: float,
        quantity_tolerance: float,
        trigger_tolerance: float,
    ) -> VerificationCoordinatorResult:
        if not isinstance(session, VerificationSession):
            raise TypeError("session must be VerificationSession")
        current = VerificationSession(
            session.operation_id,
            session.started_at,
            session.attempts_completed + 1,
        )
        try:
            snapshot = self._query.fetch(exchange_position_key)
        except Exception as exc:  # noqa: BLE001 - injected query boundary
            return self._transient(current, now, message=str(exc))
        if not isinstance(snapshot, BinanceVerificationSnapshot):
            return VerificationCoordinatorResult(
                VerificationCoordinatorCode.QUARANTINE,
                current,
                message="query port returned an invalid snapshot",
            )
        try:
            observation = normalize_binance_verification_snapshot(
                exchange_position_key=exchange_position_key,
                position_risk_payload=snapshot.position_risk_payload,
                open_algo_orders_payload=snapshot.open_algo_orders_payload,
                observed_at=snapshot.observed_at,
            )
        except (TypeError, ValueError) as exc:
            return VerificationCoordinatorResult(
                VerificationCoordinatorCode.QUARANTINE,
                current,
                message=str(exc),
            )
        if observation.code is BinanceObservationCode.POSITION_NOT_FOUND:
            return self._transient(current, now, observation=observation)
        if not observation.normalized:
            return VerificationCoordinatorResult(
                VerificationCoordinatorCode.QUARANTINE,
                current,
                observation=observation,
                message=observation.message,
            )
        commit = self._commit.commit(
            exchange_position_key=exchange_position_key,
            exposure=observation.exposure,
            orders=observation.orders,
            operation_id=session.operation_id,
            now=now,
            max_evidence_age=max_evidence_age,
            quantity_tolerance=quantity_tolerance,
            trigger_tolerance=trigger_tolerance,
        )
        if commit.applied:
            return VerificationCoordinatorResult(
                VerificationCoordinatorCode.ACTIVE,
                current,
                observation=observation,
                commit=commit,
            )
        if (
            commit.code is VerifiedActiveCommitCode.NOT_VERIFIED
            and commit.verification
        ):
            decision = decide_verification_retry(
                result=commit.verification,
                attempts_completed=current.attempts_completed,
                started_at=session.started_at,
                now=now,
                policy=self._policy,
            )
            return self._from_retry(
                decision.action,
                current,
                decision.retry_at,
                observation,
                commit,
            )
        if commit.code is VerifiedActiveCommitCode.UNAVAILABLE:
            return self._transient(
                current, now, observation=observation, commit=commit
            )
        if commit.code is VerifiedActiveCommitCode.STALE:
            code = VerificationCoordinatorCode.RELOAD_CANONICAL
        elif commit.code is VerifiedActiveCommitCode.UNKNOWN:
            code = VerificationCoordinatorCode.RESOLVE_UNKNOWN
        else:
            code = VerificationCoordinatorCode.QUARANTINE
        return VerificationCoordinatorResult(
            code, current, observation=observation, commit=commit
        )

    def _transient(
        self, session, now, *, observation=None, commit=None, message=""
    ):
        deadline = session.started_at + self._policy.deadline_seconds
        if session.attempts_completed >= self._policy.max_attempts or now >= deadline:
            code = VerificationCoordinatorCode.EXHAUSTED
            retry_at = None
        else:
            delay = min(
                self._policy.base_delay_seconds
                * (2 ** (session.attempts_completed - 1)),
                self._policy.max_delay_seconds,
            )
            retry_at = now + delay
            if retry_at > deadline:
                code = VerificationCoordinatorCode.EXHAUSTED
                retry_at = None
            else:
                code = VerificationCoordinatorCode.QUERY_AGAIN
        return VerificationCoordinatorResult(
            code, session, retry_at, observation, commit, message
        )

    @staticmethod
    def _from_retry(
        action, session, retry_at, observation=None, commit=None, message=""
    ):
        mapping = {
            VerificationRetryAction.QUERY_AGAIN: VerificationCoordinatorCode.QUERY_AGAIN,
            VerificationRetryAction.EXHAUSTED: VerificationCoordinatorCode.EXHAUSTED,
            VerificationRetryAction.QUARANTINE: VerificationCoordinatorCode.QUARANTINE,
        }
        return VerificationCoordinatorResult(
            mapping[action], session, retry_at, observation, commit, message
        )
