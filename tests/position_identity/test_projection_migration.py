"""R1 dormant legacy projection migration and mixed-writer guards."""
from dataclasses import replace

from position_identity.authority import (
    AuthorityProvenance,
    AuthorityStatus,
    SlotAuthority,
)
from position_identity.projection import (
    ProjectionReadCode,
    ProjectionReadResult,
)
from position_identity.projection_migration import (
    LegacyProjectionCode,
    LegacyProjectionReason,
    LegacyWriterDecision,
    legacy_snapshot_write_decision,
    prepare_legacy_projection,
)
from position_identity.slot import ExchangePositionKey

EPISODE = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='migration-test', environment='SANDBOX',
        symbol='BTCUSDT')


def _authority(**changes):
    values = {
        'exchange_position_key': _slot(), 'episode_id': EPISODE,
        'slot_generation': 4, 'status': AuthorityStatus.ACTIVE,
        'provenance': AuthorityProvenance.MIGRATED, 'revision': 5,
        'legacy_position_id_alias': 'S6:BTCUSDT:legacy',
        'created_at': 1, 'updated_at': 10,
    }
    values.update(changes)
    return SlotAuthority(**values)


def _row(**changes):
    values = {
        'symbol': 'BTCUSDT', 'position_id': 'S6:BTCUSDT:legacy',
        'side': 'LONG', 'system': 'S6', 'qty': 2,
        'entry': 100, 'open_time': 8,
    }
    values.update(changes)
    return values


def _missing():
    return ProjectionReadResult(ProjectionReadCode.NOT_FOUND)


def test_adopted_legacy_row_prepares_revision_one_projection():
    result = prepare_legacy_projection(
        legacy_row=_row(), authority=_authority(), canonical_read=_missing(),
        operation_id='migration-1', now=20)
    assert result.code is LegacyProjectionCode.READY
    assert result.projection.episode_id == EPISODE
    assert result.projection.slot_generation == 4
    assert result.projection.state_revision == 1
    assert result.projection.identity_provenance \
        is AuthorityProvenance.MIGRATED
    assert result.projection.legacy_position_id_alias == 'S6:BTCUSDT:legacy'


def test_unadopted_or_reconstructed_authority_is_quarantined():
    for authority in (
        _authority(status=AuthorityStatus.QUARANTINED,
                   provenance=AuthorityProvenance.RECONSTRUCTED),
        replace(_authority(), status=AuthorityStatus.FLAT),
        _authority(provenance=AuthorityProvenance.NATIVE,
                   legacy_position_id_alias=None),
    ):
        result = prepare_legacy_projection(
            legacy_row=_row(), authority=authority,
            canonical_read=_missing(), operation_id='migration-1', now=20)
        assert result.code is LegacyProjectionCode.QUARANTINE
        assert result.reason is LegacyProjectionReason.AUTHORITY_NOT_ADOPTED


def test_malformed_mismatch_and_mixed_version_rows_are_quarantined():
    cases = [
        (_row(symbol='ETHUSDT'), LegacyProjectionReason.AUTHORITY_MISMATCH),
        (_row(position_id='other'), LegacyProjectionReason.AUTHORITY_MISMATCH),
        (_row(qty=0), LegacyProjectionReason.MALFORMED_LEGACY_ROW),
        (_row(episode_id=EPISODE), LegacyProjectionReason.MIXED_VERSION),
    ]
    for row, reason in cases:
        result = prepare_legacy_projection(
            legacy_row=row, authority=_authority(), canonical_read=_missing(),
            operation_id='migration-1', now=20)
        assert result.code is LegacyProjectionCode.QUARANTINE
        assert result.reason is reason


def test_existing_exact_projection_is_idempotent_but_conflict_quarantines():
    ready = prepare_legacy_projection(
        legacy_row=_row(), authority=_authority(), canonical_read=_missing(),
        operation_id='migration-1', now=20)
    exact = ProjectionReadResult(
        ProjectionReadCode.FOUND, ready.projection)
    repeated = prepare_legacy_projection(
        legacy_row=_row(), authority=_authority(), canonical_read=exact,
        operation_id='migration-2', now=21)
    assert repeated.code is LegacyProjectionCode.ALREADY_PROJECTED

    conflict = ProjectionReadResult(
        ProjectionReadCode.FOUND,
        ready.projection.revise(operation_id='other', now=22, quantity=3))
    # Same episode is still the same migrated projection, not a new adoption.
    assert prepare_legacy_projection(
        legacy_row=_row(), authority=_authority(), canonical_read=conflict,
        operation_id='migration-3', now=23,
    ).code is LegacyProjectionCode.ALREADY_PROJECTED

    other_authority = _authority(slot_generation=5)
    mixed = prepare_legacy_projection(
        legacy_row=_row(), authority=other_authority, canonical_read=exact,
        operation_id='migration-4', now=24)
    assert mixed.reason is LegacyProjectionReason.MIXED_VERSION


def test_projection_outage_or_malformed_state_never_falls_back_to_legacy():
    for code, reason in (
        (ProjectionReadCode.UNAVAILABLE,
         LegacyProjectionReason.PROJECTION_UNAVAILABLE),
        (ProjectionReadCode.MALFORMED,
         LegacyProjectionReason.PROJECTION_MALFORMED),
    ):
        read = ProjectionReadResult(code)
        result = prepare_legacy_projection(
            legacy_row=_row(), authority=_authority(), canonical_read=read,
            operation_id='migration-1', now=20)
        assert result.code is LegacyProjectionCode.QUARANTINE
        assert result.reason is reason
        assert legacy_snapshot_write_decision(read) \
            is LegacyWriterDecision.FAIL_CLOSED


def test_legacy_writer_is_allowed_only_before_canonical_slot_exists():
    assert legacy_snapshot_write_decision(_missing()) \
        is LegacyWriterDecision.ALLOW_WHILE_UNMIGRATED
    ready = prepare_legacy_projection(
        legacy_row=_row(), authority=_authority(), canonical_read=_missing(),
        operation_id='migration-1', now=20)
    found = ProjectionReadResult(ProjectionReadCode.FOUND, ready.projection)
    assert legacy_snapshot_write_decision(found) \
        is LegacyWriterDecision.BLOCK_CANONICAL_PRESENT
