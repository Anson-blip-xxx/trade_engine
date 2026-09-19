"""ACK propagation from fenced snapshot commit into lifecycle continuation."""

import pytest

from position_identity.slot import ExchangePositionKey
from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    FencedSnapshotCommitResult,
)
from position_state.lifecycle_ack import AcknowledgedSnapshotLifecycleService
from position_state.snapshot_ack import classify_snapshot_commit


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id="ack-propagation",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


class Committer:
    def __init__(self, code):
        self.acknowledgement = classify_snapshot_commit(
            FencedSnapshotCommitResult(code)
        )
        self.calls = []

    def commit(self, **kwargs):
        self.calls.append(kwargs)
        return self.acknowledgement


@pytest.mark.parametrize("code", tuple(FencedSnapshotCommitCode))
def test_continuation_runs_only_for_acknowledged_commit_codes(code):
    committer = Committer(code)
    service = AcknowledgedSnapshotLifecycleService(committer)
    snapshot = {"BTCUSDT": {"qty": 1}}
    slots = {"BTCUSDT": _slot()}
    continued = []

    result = service.commit_and_advance(
        proposed_snapshot=snapshot,
        slots=slots,
        continuation=lambda acknowledgement: continued.append(acknowledgement)
        or "advanced",
    )

    assert committer.calls == [
        {"proposed_snapshot": snapshot, "slots": slots}
    ]
    assert result.acknowledgement is committer.acknowledgement
    assert (
        result.continuation_completed
        is committer.acknowledgement.acknowledged
    )
    if result.continuation_completed:
        assert continued == [committer.acknowledgement]
        assert result.continuation_result == "advanced"
    else:
        assert continued == []
        assert result.continuation_result is None


def test_invalid_continuation_is_rejected_before_commit():
    committer = Committer(FencedSnapshotCommitCode.APPLIED)
    service = AcknowledgedSnapshotLifecycleService(committer)
    with pytest.raises(TypeError):
        service.commit_and_advance(
            proposed_snapshot={}, slots={}, continuation=None
        )
    assert committer.calls == []


def test_invalid_commit_service_result_never_reaches_continuation():
    committer = type("InvalidCommitter", (), {"commit": lambda self, **kw: None})()
    continued = []
    with pytest.raises(TypeError):
        AcknowledgedSnapshotLifecycleService(committer).commit_and_advance(
            proposed_snapshot={},
            slots={},
            continuation=continued.append,
        )
    assert continued == []


def test_commit_and_continuation_exceptions_are_not_swallowed():
    class FailedCommitter:
        def commit(self, **_kwargs):
            raise RuntimeError("commit boundary failed")

    continued = []
    with pytest.raises(RuntimeError, match="commit boundary failed"):
        AcknowledgedSnapshotLifecycleService(FailedCommitter()).commit_and_advance(
            proposed_snapshot={}, slots={}, continuation=continued.append
        )
    assert continued == []

    service = AcknowledgedSnapshotLifecycleService(
        Committer(FencedSnapshotCommitCode.APPLIED)
    )

    def fail_continuation(_acknowledgement):
        raise RuntimeError("continuation failed")

    with pytest.raises(RuntimeError, match="continuation failed"):
        service.commit_and_advance(
            proposed_snapshot={},
            slots={},
            continuation=fail_continuation,
        )
