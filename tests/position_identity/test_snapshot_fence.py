"""Batch legacy snapshot fencing around canonical position slots."""

from uuid import uuid4

import pytest

from position_identity.authority import (
    AuthorityProvenance,
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
from position_identity.slot import ExchangePositionKey
from position_identity.snapshot_fence import (
    CanonicalSnapshotRead,
    SnapshotFenceCode,
    SnapshotFenceReason,
    plan_legacy_snapshot_write,
)


def _key(symbol="BTCUSDT"):
    return ExchangePositionKey.one_way(
        account_principal_id="writer-fence",
        environment="SANDBOX",
        symbol=symbol,
    )


def _canonical(symbol="BTCUSDT", status=AuthorityStatus.ACTIVE):
    key = _key(symbol)
    episode = str(uuid4())
    authority = SlotAuthority(
        exchange_position_key=key,
        episode_id=episode,
        slot_generation=1,
        status=status,
        provenance=AuthorityProvenance.MIGRATED,
        revision=2,
        legacy_position_id_alias=f"legacy:{symbol}",
        created_at=1,
        updated_at=2,
    )
    projection = LivePositionProjection(
        exchange_position_key=key,
        episode_id=episode,
        slot_generation=1,
        identity_provenance=AuthorityProvenance.MIGRATED,
        state_revision=1,
        last_operation_id=f"migration:{symbol}",
        side="LONG",
        system="S6",
        quantity=2,
        entry_price=100,
        opened_at=1,
        updated_at=2,
        legacy_position_id_alias=f"legacy:{symbol}",
    )
    return CanonicalSnapshotRead(
        AuthorityReadResult(AuthorityReadCode.FOUND, authority),
        ProjectionReadResult(ProjectionReadCode.FOUND, projection),
    )


def _missing():
    return CanonicalSnapshotRead(
        AuthorityReadResult(AuthorityReadCode.NOT_FOUND),
        ProjectionReadResult(ProjectionReadCode.NOT_FOUND),
    )


def _row(qty=2):
    return {"entry": 100, "qty": qty, "side": "LONG", "system": "S6"}


def test_unmigrated_batch_is_allowed_unchanged():
    proposed = {"BTCUSDT": _row(), "ETHUSDT": _row(3)}
    plan = plan_legacy_snapshot_write(
        current_snapshot={"BTCUSDT": _row()},
        proposed_snapshot=proposed,
        canonical_reads={"BTCUSDT": _missing(), "ETHUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.ALLOW_ALL
    assert dict(plan.snapshot) == proposed
    assert plan.fenced_symbols == ()


def test_active_canonical_row_may_pass_only_when_exactly_unchanged():
    current = {"BTCUSDT": _row(), "ETHUSDT": _row(3)}
    proposed = {"BTCUSDT": _row(), "ETHUSDT": _row(4)}
    plan = plan_legacy_snapshot_write(
        current_snapshot=current,
        proposed_snapshot=proposed,
        canonical_reads={"BTCUSDT": _canonical(), "ETHUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.ALLOW_ALL
    assert plan.snapshot["ETHUSDT"]["qty"] == 4


@pytest.mark.parametrize("proposed", [{"BTCUSDT": _row(1)}, {}])
def test_active_canonical_mutation_is_filtered_without_losing_other_updates(proposed):
    current = {"BTCUSDT": _row(), "ETHUSDT": _row(3)}
    proposed = {**proposed, "ETHUSDT": _row(4)}
    plan = plan_legacy_snapshot_write(
        current_snapshot=current,
        proposed_snapshot=proposed,
        canonical_reads={"BTCUSDT": _canonical(), "ETHUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.FILTERED
    assert plan.snapshot["BTCUSDT"] == current["BTCUSDT"]
    assert plan.snapshot["ETHUSDT"]["qty"] == 4
    assert plan.reasons["BTCUSDT"] is SnapshotFenceReason.ACTIVE_CANONICAL_MUTATION


def test_canonical_active_row_cannot_be_reintroduced_by_legacy_writer():
    plan = plan_legacy_snapshot_write(
        current_snapshot={},
        proposed_snapshot={"BTCUSDT": _row()},
        canonical_reads={"BTCUSDT": _canonical()},
    )
    assert plan.code is SnapshotFenceCode.FILTERED
    assert dict(plan.snapshot) == {}


def test_flat_canonical_row_is_removed_instead_of_reintroduced():
    read = _canonical()
    flat = read.authority.authority.transition_to_flat(now=3)
    read = CanonicalSnapshotRead(
        AuthorityReadResult(AuthorityReadCode.FOUND, flat), read.projection
    )
    plan = plan_legacy_snapshot_write(
        current_snapshot={"BTCUSDT": _row()},
        proposed_snapshot={"BTCUSDT": _row(), "ETHUSDT": _row(4)},
        canonical_reads={"BTCUSDT": read, "ETHUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.FILTERED
    assert "BTCUSDT" not in plan.snapshot
    assert "ETHUSDT" in plan.snapshot
    assert plan.reasons["BTCUSDT"] is (
        SnapshotFenceReason.FLAT_CANONICAL_REINTRODUCTION
    )


@pytest.mark.parametrize(
    ("read", "reason"),
    [
        (
            CanonicalSnapshotRead(
                AuthorityReadResult(AuthorityReadCode.BACKEND_ERROR),
                ProjectionReadResult(ProjectionReadCode.UNAVAILABLE),
            ),
            SnapshotFenceReason.CANONICAL_UNAVAILABLE,
        ),
        (
            CanonicalSnapshotRead(
                AuthorityReadResult(AuthorityReadCode.MALFORMED),
                ProjectionReadResult(ProjectionReadCode.MALFORMED),
            ),
            SnapshotFenceReason.CANONICAL_MALFORMED,
        ),
        (
            CanonicalSnapshotRead(
                AuthorityReadResult(AuthorityReadCode.NOT_FOUND),
                _canonical().projection,
            ),
            SnapshotFenceReason.AUTHORITY_PROJECTION_MISMATCH,
        ),
    ],
)
def test_unclassifiable_canonical_state_fails_entire_batch(read, reason):
    plan = plan_legacy_snapshot_write(
        current_snapshot={"BTCUSDT": _row()},
        proposed_snapshot={"BTCUSDT": _row(1), "ETHUSDT": _row(4)},
        canonical_reads={"BTCUSDT": read, "ETHUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.FAIL_CLOSED
    assert plan.snapshot is None
    assert plan.reasons["BTCUSDT"] is reason


def test_incomplete_read_set_fails_closed():
    plan = plan_legacy_snapshot_write(
        current_snapshot={"BTCUSDT": _row()},
        proposed_snapshot={"BTCUSDT": _row(), "ETHUSDT": _row()},
        canonical_reads={"BTCUSDT": _missing()},
    )
    assert plan.code is SnapshotFenceCode.FAIL_CLOSED
    assert plan.message == SnapshotFenceReason.INCOMPLETE_READ_SET.value


def test_plan_and_inputs_are_not_mutable_aliases():
    current = {"BTCUSDT": _row()}
    proposed = {"BTCUSDT": {**_row(1), "metadata": {"tag": "before"}}}
    plan = plan_legacy_snapshot_write(
        current_snapshot=current,
        proposed_snapshot=proposed,
        canonical_reads={"BTCUSDT": _canonical()},
    )
    proposed["BTCUSDT"]["qty"] = 99
    proposed["BTCUSDT"]["metadata"]["tag"] = "after"
    assert plan.snapshot["BTCUSDT"]["qty"] == 2
    assert "metadata" not in plan.snapshot["BTCUSDT"]
    with pytest.raises(TypeError):
        plan.snapshot["ETHUSDT"] = _row()
