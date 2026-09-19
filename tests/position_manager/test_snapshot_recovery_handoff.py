"""Blocked snapshot outcome recovery-handoff contract tests."""

import pytest

from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    FencedSnapshotCommitResult,
)
from position_state.lifecycle_ack import AcknowledgedSnapshotLifecycleService
from position_state.recovery_handoff import (
    RecoverableSnapshotLifecycleService,
    SnapshotRecoveryHandoffCode,
    SnapshotRecoveryHandoffResult,
)
from position_state.snapshot_ack import classify_snapshot_commit


class Committer:
    def __init__(self, code):
        self.ack = classify_snapshot_commit(FencedSnapshotCommitResult(code))
        self.calls = []

    def commit(self, **kwargs):
        self.calls.append(kwargs)
        return self.ack


class RecoveryPort:
    def __init__(self, code=SnapshotRecoveryHandoffCode.ACKNOWLEDGED):
        self.result = SnapshotRecoveryHandoffResult(code)
        self.calls = []

    def handoff(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _service(code, recovery=None):
    committer = Committer(code)
    recovery = recovery or RecoveryPort()
    service = RecoverableSnapshotLifecycleService(
        lifecycle_service=AcknowledgedSnapshotLifecycleService(committer),
        recovery_port=recovery,
    )
    return service, committer, recovery


@pytest.mark.parametrize(
    "code",
    (
        FencedSnapshotCommitCode.APPLIED,
        FencedSnapshotCommitCode.ALREADY_APPLIED,
        FencedSnapshotCommitCode.FILTERED_APPLIED,
        FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED,
    ),
)
def test_acknowledged_snapshot_continues_without_recovery_handoff(code):
    service, _committer, recovery = _service(code)
    continued = []
    result = service.commit_and_route(
        operation_id="op-1",
        proposed_snapshot={},
        slots={},
        continuation=continued.append,
    )
    assert result.continuation_completed
    assert result.recovery is None
    assert not result.recovery_acknowledged
    assert len(continued) == 1
    assert recovery.calls == []


@pytest.mark.parametrize(
    "code",
    tuple(
        code
        for code in FencedSnapshotCommitCode
        if code
        not in {
            FencedSnapshotCommitCode.APPLIED,
            FencedSnapshotCommitCode.ALREADY_APPLIED,
            FencedSnapshotCommitCode.FILTERED_APPLIED,
            FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED,
        }
    ),
)
def test_every_blocked_outcome_hands_off_exact_operation_once(code):
    service, committer, recovery = _service(code)
    snapshot = {"BTCUSDT": {"qty": 1}}
    slots = {"BTCUSDT": object()}
    continued = []
    result = service.commit_and_route(
        operation_id=" op-2 ",
        proposed_snapshot=snapshot,
        slots=slots,
        continuation=continued.append,
    )
    assert not result.continuation_completed
    assert result.recovery_acknowledged
    assert continued == []
    assert len(committer.calls) == 1
    assert recovery.calls == [
        {
            "operation_id": "op-2",
            "acknowledgement": committer.ack,
            "proposed_snapshot": snapshot,
            "slots": slots,
        }
    ]


@pytest.mark.parametrize("code", tuple(SnapshotRecoveryHandoffCode))
def test_recovery_ack_is_never_inferred_from_non_ack_codes(code):
    service, _committer, _recovery = _service(
        FencedSnapshotCommitCode.UNKNOWN, RecoveryPort(code)
    )
    result = service.commit_and_route(
        operation_id="op-3",
        proposed_snapshot={},
        slots={},
        continuation=lambda _ack: None,
    )
    assert result.recovery.code is code
    assert result.recovery_acknowledged is (
        code is SnapshotRecoveryHandoffCode.ACKNOWLEDGED
    )


@pytest.mark.parametrize("operation_id", [None, "", "  ", "bad\nvalue"])
def test_invalid_operation_id_fails_before_commit_or_handoff(operation_id):
    service, committer, recovery = _service(FencedSnapshotCommitCode.UNKNOWN)
    with pytest.raises(ValueError):
        service.commit_and_route(
            operation_id=operation_id,
            proposed_snapshot={},
            slots={},
            continuation=lambda _ack: None,
        )
    assert committer.calls == []
    assert recovery.calls == []


def test_invalid_or_failed_recovery_port_is_not_coerced_to_acknowledged():
    class InvalidRecovery:
        def handoff(self, **_kwargs):
            return None

    service, _committer, _recovery = _service(
        FencedSnapshotCommitCode.UNKNOWN, InvalidRecovery()
    )
    with pytest.raises(TypeError):
        service.commit_and_route(
            operation_id="op-4",
            proposed_snapshot={},
            slots={},
            continuation=lambda _ack: None,
        )

    class FailedRecovery:
        def handoff(self, **_kwargs):
            raise RuntimeError("durable sink unavailable")

    service, _committer, _recovery = _service(
        FencedSnapshotCommitCode.UNKNOWN, FailedRecovery()
    )
    with pytest.raises(RuntimeError, match="durable sink unavailable"):
        service.commit_and_route(
            operation_id="op-5",
            proposed_snapshot={},
            slots={},
            continuation=lambda _ack: None,
        )


def test_invalid_result_code_and_missing_handoff_fail_fast():
    with pytest.raises(TypeError):
        SnapshotRecoveryHandoffResult("ACKNOWLEDGED")
    with pytest.raises(TypeError):
        SnapshotRecoveryHandoffResult(
            SnapshotRecoveryHandoffCode.ACKNOWLEDGED, message=None
        )
    committer = Committer(FencedSnapshotCommitCode.UNKNOWN)
    with pytest.raises(TypeError):
        RecoverableSnapshotLifecycleService(
            lifecycle_service=AcknowledgedSnapshotLifecycleService(committer),
            recovery_port=object(),
        )
    assert committer.calls == []
