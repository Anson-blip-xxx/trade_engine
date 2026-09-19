"""Durable-sink-neutral handoff for blocked snapshot lifecycle outcomes."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from position_identity.slot import ExchangePositionKey
from position_state.lifecycle_ack import (
    AcknowledgedSnapshotLifecycleService,
    SnapshotLifecycleResult,
)
from position_state.snapshot_ack import SnapshotCommitAcknowledgement


class SnapshotRecoveryHandoffCode(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class SnapshotRecoveryHandoffResult:
    code: SnapshotRecoveryHandoffCode
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, SnapshotRecoveryHandoffCode):
            raise TypeError("code must be SnapshotRecoveryHandoffCode")
        if not isinstance(self.message, str):
            raise TypeError("message must be text")

    @property
    def acknowledged(self) -> bool:
        return self.code is SnapshotRecoveryHandoffCode.ACKNOWLEDGED


class SnapshotRecoveryHandoffPort(Protocol):
    def handoff(
        self,
        *,
        operation_id: str,
        acknowledgement: SnapshotCommitAcknowledgement,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
    ) -> SnapshotRecoveryHandoffResult: ...


@dataclass(frozen=True)
class RecoverableSnapshotLifecycleResult:
    lifecycle: SnapshotLifecycleResult
    recovery: SnapshotRecoveryHandoffResult | None = None

    @property
    def continuation_completed(self) -> bool:
        return self.lifecycle.continuation_completed

    @property
    def recovery_acknowledged(self) -> bool:
        return self.recovery is not None and self.recovery.acknowledged


class RecoverableSnapshotLifecycleService:
    """Continue after ACK; otherwise hand off the exact blocked operation once."""

    def __init__(self, *, lifecycle_service, recovery_port) -> None:
        if not isinstance(lifecycle_service, AcknowledgedSnapshotLifecycleService):
            raise TypeError(
                "lifecycle_service must be AcknowledgedSnapshotLifecycleService"
            )
        if not callable(getattr(recovery_port, "handoff", None)):
            raise TypeError("recovery_port must provide callable handoff")
        self._lifecycle = lifecycle_service
        self._recovery = recovery_port

    def commit_and_route(
        self,
        *,
        operation_id: str,
        proposed_snapshot: dict,
        slots: Mapping[str, ExchangePositionKey],
        continuation: Callable[[SnapshotCommitAcknowledgement], object],
    ) -> RecoverableSnapshotLifecycleResult:
        operation_id = self._operation_id(operation_id)
        lifecycle = self._lifecycle.commit_and_advance(
            proposed_snapshot=proposed_snapshot,
            slots=slots,
            continuation=continuation,
        )
        if lifecycle.continuation_completed:
            return RecoverableSnapshotLifecycleResult(lifecycle)
        recovery = self._recovery.handoff(
            operation_id=operation_id,
            acknowledgement=lifecycle.acknowledgement,
            proposed_snapshot=proposed_snapshot,
            slots=slots,
        )
        if not isinstance(recovery, SnapshotRecoveryHandoffResult):
            raise TypeError(
                "recovery port must return SnapshotRecoveryHandoffResult"
            )
        return RecoverableSnapshotLifecycleResult(lifecycle, recovery)

    @staticmethod
    def _operation_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("operation_id is required")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("operation_id contains control characters")
        return value.strip()
