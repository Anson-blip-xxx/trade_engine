"""ACK-propagating caller boundary for dormant snapshot lifecycle stages."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from position_identity.slot import ExchangePositionKey
from position_state.snapshot_ack import SnapshotCommitAcknowledgement


class SnapshotCommitServicePort(Protocol):
    def commit(
        self,
        *,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
    ) -> SnapshotCommitAcknowledgement: ...


@dataclass(frozen=True)
class SnapshotLifecycleResult:
    acknowledgement: SnapshotCommitAcknowledgement
    continuation_result: object | None = None

    @property
    def continuation_completed(self) -> bool:
        return self.acknowledgement.acknowledged


class AcknowledgedSnapshotLifecycleService:
    """Commit once and invoke continuation only after an acknowledged write."""

    def __init__(self, commit_service: SnapshotCommitServicePort) -> None:
        self._commit_service = commit_service

    def commit_and_advance(
        self,
        *,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
        continuation: Callable[[SnapshotCommitAcknowledgement], object],
    ) -> SnapshotLifecycleResult:
        if not callable(continuation):
            raise TypeError("continuation must be callable")
        acknowledgement = self._commit_service.commit(
            proposed_snapshot=proposed_snapshot,
            slots=slots,
        )
        if not isinstance(acknowledgement, SnapshotCommitAcknowledgement):
            raise TypeError(
                "commit service must return SnapshotCommitAcknowledgement"
            )
        if acknowledgement.blocks_lifecycle:
            return SnapshotLifecycleResult(acknowledgement)
        continuation_result = continuation(acknowledgement)
        return SnapshotLifecycleResult(acknowledgement, continuation_result)
