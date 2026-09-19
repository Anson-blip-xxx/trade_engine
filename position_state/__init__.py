"""Position state services and strict typed snapshot boundary."""

from position_state.fenced_snapshot import (
    FencedSnapshotCommitCode,
    FencedSnapshotCommitResult,
    RedisFencedSnapshotCommitAdapter,
)
from position_state.lifecycle_ack import (
    AcknowledgedSnapshotLifecycleService,
    SnapshotLifecycleResult,
)
from position_state.recovery_handoff import (
    RecoverableSnapshotLifecycleResult,
    RecoverableSnapshotLifecycleService,
    SnapshotRecoveryHandoffCode,
    SnapshotRecoveryHandoffResult,
)
from position_state.snapshot_ack import (
    FencedSnapshotCommitService,
    SnapshotCommitAcknowledgement,
    SnapshotCommitAction,
    classify_snapshot_commit,
)
from position_state.strict_snapshot import (
    StrictRedisPositionSnapshotAdapter,
    StrictSnapshotRead,
    StrictSnapshotReadCode,
    StrictSnapshotWriteCode,
    StrictSnapshotWriteResult,
)

__all__ = [
    "AcknowledgedSnapshotLifecycleService",
    "FencedSnapshotCommitCode",
    "FencedSnapshotCommitResult",
    "FencedSnapshotCommitService",
    "RecoverableSnapshotLifecycleResult",
    "RecoverableSnapshotLifecycleService",
    "RedisFencedSnapshotCommitAdapter",
    "SnapshotCommitAcknowledgement",
    "SnapshotCommitAction",
    "SnapshotLifecycleResult",
    "SnapshotRecoveryHandoffCode",
    "SnapshotRecoveryHandoffResult",
    "StrictRedisPositionSnapshotAdapter",
    "StrictSnapshotRead",
    "StrictSnapshotReadCode",
    "StrictSnapshotWriteCode",
    "StrictSnapshotWriteResult",
    "classify_snapshot_commit",
]
