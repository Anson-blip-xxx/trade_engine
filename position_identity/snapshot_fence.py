"""Pure batch planner that fences legacy snapshots around canonical slots."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from position_identity.authority import (
    AuthorityReadCode,
    AuthorityReadResult,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import (
    LivePositionProjection,
    ProjectionReadCode,
    ProjectionReadResult,
)


class SnapshotFenceCode(str, Enum):
    ALLOW_ALL = "ALLOW_ALL"
    FILTERED = "FILTERED"
    FAIL_CLOSED = "FAIL_CLOSED"
    INVALID = "INVALID"


class SnapshotFenceReason(str, Enum):
    ACTIVE_CANONICAL_MUTATION = "ACTIVE_CANONICAL_MUTATION"
    FLAT_CANONICAL_REINTRODUCTION = "FLAT_CANONICAL_REINTRODUCTION"
    AUTHORITY_PROJECTION_MISMATCH = "AUTHORITY_PROJECTION_MISMATCH"
    CANONICAL_UNAVAILABLE = "CANONICAL_UNAVAILABLE"
    CANONICAL_MALFORMED = "CANONICAL_MALFORMED"
    INCOMPLETE_READ_SET = "INCOMPLETE_READ_SET"
    MALFORMED_SNAPSHOT = "MALFORMED_SNAPSHOT"


@dataclass(frozen=True)
class CanonicalSnapshotRead:
    authority: AuthorityReadResult
    projection: ProjectionReadResult


@dataclass(frozen=True)
class LegacySnapshotWritePlan:
    code: SnapshotFenceCode
    snapshot: Mapping[str, dict] | None = None
    fenced_symbols: tuple[str, ...] = ()
    reasons: Mapping[str, SnapshotFenceReason] = MappingProxyType({})
    message: str = ""

    @property
    def writable(self) -> bool:
        return self.code in (
            SnapshotFenceCode.ALLOW_ALL,
            SnapshotFenceCode.FILTERED,
        )


def plan_legacy_snapshot_write(
    *,
    current_snapshot: dict,
    proposed_snapshot: dict,
    canonical_reads: Mapping[str, CanonicalSnapshotRead],
) -> LegacySnapshotWritePlan:
    """Plan one whole-snapshot write without mutating any input mapping.

    Every symbol in the current/proposed union requires an explicit canonical
    read pair. Canonical ACTIVE/QUARANTINED rows are immutable through the
    legacy writer; canonical FLAT rows are removed rather than reintroduced.
    """
    if not _valid_snapshot(current_snapshot) or not _valid_snapshot(
        proposed_snapshot
    ):
        return _failure(
            SnapshotFenceCode.INVALID,
            SnapshotFenceReason.MALFORMED_SNAPSHOT,
        )
    if not isinstance(canonical_reads, Mapping):
        return _failure(
            SnapshotFenceCode.INVALID,
            SnapshotFenceReason.INCOMPLETE_READ_SET,
        )

    symbols = set(current_snapshot) | set(proposed_snapshot)
    if set(canonical_reads) != symbols:
        return _failure(
            SnapshotFenceCode.FAIL_CLOSED,
            SnapshotFenceReason.INCOMPLETE_READ_SET,
        )

    output = deepcopy(proposed_snapshot)
    fenced: dict[str, SnapshotFenceReason] = {}
    for symbol in sorted(symbols):
        read = canonical_reads.get(symbol)
        state = _canonical_state(symbol, read)
        if isinstance(state, SnapshotFenceReason):
            return _failure(SnapshotFenceCode.FAIL_CLOSED, state, symbol)
        if state is None:
            continue

        authority, _projection = state
        if authority.status in (
            AuthorityStatus.ACTIVE,
            AuthorityStatus.QUARANTINED,
        ):
            current = current_snapshot.get(symbol)
            proposed = proposed_snapshot.get(symbol)
            if current == proposed:
                continue
            if current is None:
                output.pop(symbol, None)
            else:
                output[symbol] = deepcopy(current)
            fenced[symbol] = SnapshotFenceReason.ACTIVE_CANONICAL_MUTATION
            continue

        assert authority.status is AuthorityStatus.FLAT
        if symbol in output:
            output.pop(symbol, None)
            fenced[symbol] = SnapshotFenceReason.FLAT_CANONICAL_REINTRODUCTION

    code = SnapshotFenceCode.FILTERED if fenced else SnapshotFenceCode.ALLOW_ALL
    return LegacySnapshotWritePlan(
        code=code,
        snapshot=MappingProxyType(output),
        fenced_symbols=tuple(fenced),
        reasons=MappingProxyType(fenced),
    )


def _canonical_state(symbol, read):
    if not isinstance(read, CanonicalSnapshotRead):
        return SnapshotFenceReason.INCOMPLETE_READ_SET
    authority_read = read.authority
    projection_read = read.projection
    if not isinstance(authority_read, AuthorityReadResult) or not isinstance(
        projection_read, ProjectionReadResult
    ):
        return SnapshotFenceReason.CANONICAL_MALFORMED
    if authority_read.code is AuthorityReadCode.BACKEND_ERROR or (
        projection_read.code is ProjectionReadCode.UNAVAILABLE
    ):
        return SnapshotFenceReason.CANONICAL_UNAVAILABLE
    if authority_read.code is AuthorityReadCode.MALFORMED or (
        projection_read.code is ProjectionReadCode.MALFORMED
    ):
        return SnapshotFenceReason.CANONICAL_MALFORMED
    if projection_read.code is ProjectionReadCode.NOT_FOUND:
        if authority_read.code is AuthorityReadCode.NOT_FOUND:
            return None
        authority = authority_read.authority
        if (
            authority_read.code is AuthorityReadCode.FOUND
            and isinstance(authority, SlotAuthority)
            and authority.status is AuthorityStatus.FLAT
            and authority.episode_id is None
            and authority.slot_generation == 0
        ):
            return None
        return SnapshotFenceReason.AUTHORITY_PROJECTION_MISMATCH
    if projection_read.code is not ProjectionReadCode.FOUND:
        return SnapshotFenceReason.CANONICAL_MALFORMED
    if authority_read.code is not AuthorityReadCode.FOUND:
        return SnapshotFenceReason.AUTHORITY_PROJECTION_MISMATCH
    authority = authority_read.authority
    projection = projection_read.projection
    if not isinstance(authority, SlotAuthority) or not isinstance(
        projection, LivePositionProjection
    ):
        return SnapshotFenceReason.CANONICAL_MALFORMED
    if (
        authority.exchange_position_key != projection.exchange_position_key
        or authority.exchange_position_key.symbol != symbol
        or authority.episode_id != projection.episode_id
        or authority.slot_generation != projection.slot_generation
        or authority.provenance is not projection.identity_provenance
        or authority.legacy_position_id_alias
        != projection.legacy_position_id_alias
    ):
        return SnapshotFenceReason.AUTHORITY_PROJECTION_MISMATCH
    return authority, projection


def _valid_snapshot(snapshot) -> bool:
    return isinstance(snapshot, dict) and all(
        isinstance(symbol, str)
        and bool(symbol.strip())
        and symbol == symbol.strip().upper()
        and isinstance(row, dict)
        for symbol, row in snapshot.items()
    )


def _failure(code, reason, symbol: str | None = None):
    reasons = {} if symbol is None else {symbol: reason}
    return LegacySnapshotWritePlan(
        code=code,
        fenced_symbols=tuple(reasons),
        reasons=MappingProxyType(reasons),
        message=reason.value,
    )
