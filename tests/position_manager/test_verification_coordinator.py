"""One-step protection verification coordinator behavior."""

from dataclasses import dataclass

import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.verification import (
    ProtectionVerificationCode,
    ProtectionVerificationResult,
)
from position_protection.verification_commit import (
    VerifiedActiveCommitCode,
    VerifiedActiveCommitResult,
)
from position_protection.verification_coordinator import (
    BinanceVerificationSnapshot,
    ProtectionVerificationCoordinator,
    VerificationCoordinatorCode,
    VerificationSession,
)
from position_protection.verification_retry import VerificationRetryPolicy


def _key():
    return ExchangePositionKey.one_way(
        account_principal_id="coordinator",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


def _snapshot(position=True, orders=True):
    positions = (
        [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "2"}]
        if position
        else []
    )
    algo = (
        [
            {
                "algoId": 77,
                "algoType": "CONDITIONAL",
                "orderType": "STOP_MARKET",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "quantity": "2",
                "algoStatus": "WORKING",
                "triggerPrice": "90",
                "closePosition": False,
                "reduceOnly": True,
            }
        ]
        if orders
        else []
    )
    return BinanceVerificationSnapshot(positions, algo, 11)


@dataclass
class _Query:
    value: object
    calls: int = 0

    def fetch(self, _key):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


@dataclass
class _Commit:
    value: VerifiedActiveCommitResult
    calls: list | None = None

    def commit(self, **kwargs):
        if self.calls is None:
            self.calls = []
        self.calls.append(kwargs)
        return self.value


def _coordinator(query, commit):
    return ProtectionVerificationCoordinator(
        query_port=query,
        commit_port=commit,
        retry_policy=VerificationRetryPolicy(
            max_attempts=3,
            deadline_seconds=20,
            base_delay_seconds=2,
            max_delay_seconds=4,
        ),
    )


def _step(coordinator, session=None, now=12):
    return coordinator.step(
        session=session or VerificationSession("verify:1", 10),
        exchange_position_key=_key(),
        now=now,
        max_evidence_age=5,
        quantity_tolerance=0,
        trigger_tolerance=0,
    )


@pytest.mark.parametrize(
    "code",
    [VerifiedActiveCommitCode.APPLIED, VerifiedActiveCommitCode.ALREADY_ACTIVE],
)
def test_acknowledged_commit_is_active(code):
    commit = _Commit(VerifiedActiveCommitResult(code))
    result = _step(_coordinator(_Query(_snapshot()), commit))
    assert result.code is VerificationCoordinatorCode.ACTIVE
    assert result.session.attempts_completed == 1
    assert commit.calls[0]["operation_id"] == "verify:1"


def test_query_exception_is_bounded_without_commit():
    commit = _Commit(VerifiedActiveCommitResult(VerifiedActiveCommitCode.APPLIED))
    result = _step(_coordinator(_Query(ConnectionError("down")), commit))
    assert result.code is VerificationCoordinatorCode.QUERY_AGAIN
    assert result.retry_at == 14
    assert commit.calls is None


def test_missing_position_retries_but_malformed_payload_quarantines():
    commit = _Commit(VerifiedActiveCommitResult(VerifiedActiveCommitCode.APPLIED))
    missing = _step(_coordinator(_Query(_snapshot(position=False)), commit))
    malformed = _step(
        _coordinator(_Query(BinanceVerificationSnapshot({}, [], 11)), commit)
    )
    assert missing.code is VerificationCoordinatorCode.QUERY_AGAIN
    assert malformed.code is VerificationCoordinatorCode.QUARANTINE
    assert commit.calls is None


@pytest.mark.parametrize(
    "verification",
    [
        ProtectionVerificationCode.ORDER_NOT_FOUND,
        ProtectionVerificationCode.ORDER_NOT_ACTIVE,
        ProtectionVerificationCode.STALE_EVIDENCE,
    ],
)
def test_transient_not_verified_result_queries_again(verification):
    commit = _Commit(
        VerifiedActiveCommitResult(
            VerifiedActiveCommitCode.NOT_VERIFIED,
            verification=ProtectionVerificationResult(verification),
        )
    )
    assert _step(_coordinator(_Query(_snapshot()), commit)).code \
        is VerificationCoordinatorCode.QUERY_AGAIN


def test_hard_not_verified_result_quarantines():
    commit = _Commit(
        VerifiedActiveCommitResult(
            VerifiedActiveCommitCode.NOT_VERIFIED,
            verification=ProtectionVerificationResult(
                ProtectionVerificationCode.ORDER_SPEC_MISMATCH
            ),
        )
    )
    assert _step(_coordinator(_Query(_snapshot()), commit)).code \
        is VerificationCoordinatorCode.QUARANTINE


@pytest.mark.parametrize(
    ("commit_code", "coordinator_code"),
    [
        (VerifiedActiveCommitCode.STALE, VerificationCoordinatorCode.RELOAD_CANONICAL),
        (VerifiedActiveCommitCode.UNKNOWN, VerificationCoordinatorCode.RESOLVE_UNKNOWN),
        (VerifiedActiveCommitCode.INVALID, VerificationCoordinatorCode.QUARANTINE),
    ],
)
def test_commit_failures_preserve_distinct_caller_actions(
    commit_code, coordinator_code
):
    commit = _Commit(VerifiedActiveCommitResult(commit_code))
    assert _step(_coordinator(_Query(_snapshot()), commit)).code is coordinator_code


def test_unavailable_commit_uses_same_bounded_budget():
    commit = _Commit(VerifiedActiveCommitResult(VerifiedActiveCommitCode.UNAVAILABLE))
    session = VerificationSession("verify:1", 10, attempts_completed=2)
    result = _step(_coordinator(_Query(_snapshot()), commit), session=session, now=15)
    assert result.code is VerificationCoordinatorCode.EXHAUSTED


def test_step_never_loops_or_sleeps():
    query = _Query(_snapshot())
    commit = _Commit(VerifiedActiveCommitResult(VerifiedActiveCommitCode.APPLIED))
    _step(_coordinator(query, commit))
    assert query.calls == 1
    assert len(commit.calls) == 1
