"""Typed caller policy for fenced legacy snapshot acknowledgements."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from position_identity.slot import ExchangePositionKey
from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    FencedSnapshotCommitResult,
)


class SnapshotCommitAction(str, Enum):
    """The only caller actions permitted for a commit outcome."""

    ADVANCE = "ADVANCE"
    RELOAD_AND_REPLAN = "RELOAD_AND_REPLAN"
    RETRY_WHEN_AVAILABLE = "RETRY_WHEN_AVAILABLE"
    RESOLVE_UNKNOWN = "RESOLVE_UNKNOWN"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


@dataclass(frozen=True)
class SnapshotCommitAcknowledgement:
    result: FencedSnapshotCommitResult
    action: SnapshotCommitAction

    @property
    def acknowledged(self) -> bool:
        return self.action is SnapshotCommitAction.ADVANCE

    @property
    def blocks_lifecycle(self) -> bool:
        return not self.acknowledged


class FencedSnapshotCommitPort(Protocol):
    def commit(
        self,
        *,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
    ) -> FencedSnapshotCommitResult: ...


_ACKNOWLEDGED_CODES = frozenset(
    {
        FencedSnapshotCommitCode.APPLIED,
        FencedSnapshotCommitCode.ALREADY_APPLIED,
        FencedSnapshotCommitCode.FILTERED_APPLIED,
        FencedSnapshotCommitCode.FILTERED_ALREADY_APPLIED,
    }
)


def classify_snapshot_commit(
    result: FencedSnapshotCommitResult,
) -> SnapshotCommitAcknowledgement:
    """Convert persistence outcome to a mandatory caller control decision."""
    if not isinstance(result, FencedSnapshotCommitResult):
        raise TypeError("result must be FencedSnapshotCommitResult")
    if result.code in _ACKNOWLEDGED_CODES:
        action = SnapshotCommitAction.ADVANCE
    elif result.code is FencedSnapshotCommitCode.STALE:
        action = SnapshotCommitAction.RELOAD_AND_REPLAN
    elif result.code is FencedSnapshotCommitCode.UNAVAILABLE:
        action = SnapshotCommitAction.RETRY_WHEN_AVAILABLE
    elif result.code is FencedSnapshotCommitCode.UNKNOWN:
        action = SnapshotCommitAction.RESOLVE_UNKNOWN
    elif result.code in (
        FencedSnapshotCommitCode.FENCE_REJECTED,
        FencedSnapshotCommitCode.MALFORMED,
    ):
        action = SnapshotCommitAction.QUARANTINE
    else:
        action = SnapshotCommitAction.REJECT
    return SnapshotCommitAcknowledgement(result=result, action=action)


class FencedSnapshotCommitService:
    """Propagate every typed commit result without swallowing or coercion."""

    def __init__(self, port: FencedSnapshotCommitPort) -> None:
        self._port = port

    def commit(
        self,
        *,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
    ) -> SnapshotCommitAcknowledgement:
        result = self._port.commit(
            proposed_snapshot=proposed_snapshot,
            slots=slots,
        )
        return classify_snapshot_commit(result)
