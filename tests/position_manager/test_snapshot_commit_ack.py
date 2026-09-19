"""Typed caller policy for fenced snapshot commit acknowledgements."""

from dataclasses import dataclass

import pytest

from position_identity.slot import ExchangePositionKey
from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    FencedSnapshotCommitResult,
)
from position_state.snapshot_ack import (
    FencedSnapshotCommitService,
    SnapshotCommitAction,
    classify_snapshot_commit,
)


@pytest.mark.parametrize(
    ("code", "action"),
    [
        (FencedSnapshotCommitCode.APPLIED, SnapshotCommitAction.ADVANCE),
        (
            FencedSnapshotCommitCode.ALREADY_APPLIED,
            SnapshotCommitAction.ADVANCE,
        ),
        (
            FencedSnapshotCommitCode.FILTERED_APPLIED,
            SnapshotCommitAction.ADVANCE,
        ),
        (
            FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED,
            SnapshotCommitAction.ADVANCE,
        ),
        (
            FencedSnapshotCommitCode.STALE,
            SnapshotCommitAction.RELOAD_AND_REPLAN,
        ),
        (
            FencedSnapshotCommitCode.UNAVAILABLE,
            SnapshotCommitAction.RETRY_WHEN_AVAILABLE,
        ),
        (
            FencedSnapshotCommitCode.UNKNOWN,
            SnapshotCommitAction.RESOLVE_UNKNOWN,
        ),
        (
            FencedSnapshotCommitCode.FENCE_REJECTED,
            SnapshotCommitAction.QUARANTINE,
        ),
        (
            FencedSnapshotCommitCode.MALFORMED,
            SnapshotCommitAction.QUARANTINE,
        ),
        (FencedSnapshotCommitCode.INVALID, SnapshotCommitAction.REJECT),
    ],
)
def test_every_commit_code_has_one_explicit_caller_action(code, action):
    acknowledgement = classify_snapshot_commit(FencedSnapshotCommitResult(code))
    assert acknowledgement.action is action
    assert acknowledgement.acknowledged is (
        action is SnapshotCommitAction.ADVANCE
    )
    assert acknowledgement.blocks_lifecycle is not acknowledgement.acknowledged


def test_classifier_rejects_untyped_results():
    with pytest.raises(TypeError, match="FencedSnapshotCommitResult"):
        classify_snapshot_commit(None)


def _slot(symbol="BTCUSDT"):
    return ExchangePositionKey.one_way(
        account_principal_id="snapshot-ack",
        environment="SANDBOX",
        symbol=symbol,
    )


@dataclass
class _RecordingPort:
    result: FencedSnapshotCommitResult
    call: tuple | None = None

    def commit(self, *, proposed_snapshot, slots):
        self.call = (proposed_snapshot, slots)
        return self.result


def test_service_propagates_exact_input_and_typed_acknowledgement():
    result = FencedSnapshotCommitResult(
        FencedSnapshotCommitCode.STALE,
        positions={"BTCUSDT": {"qty": 3}},
        message="concurrent writer",
    )
    port = _RecordingPort(result)
    service = FencedSnapshotCommitService(port)
    proposed = {"BTCUSDT": {"qty": 2}}
    slots = {"BTCUSDT": _slot()}

    acknowledgement = service.commit(
        proposed_snapshot=proposed,
        slots=slots,
    )

    assert port.call == (proposed, slots)
    assert acknowledgement.result is result
    assert acknowledgement.action is SnapshotCommitAction.RELOAD_AND_REPLAN


def test_service_does_not_swallow_unexpected_port_failure():
    class BrokenPort:
        def commit(self, **_kwargs):
            raise RuntimeError("contract violation")

    with pytest.raises(RuntimeError, match="contract violation"):
        FencedSnapshotCommitService(BrokenPort()).commit(
            proposed_snapshot={}, slots={}
        )
