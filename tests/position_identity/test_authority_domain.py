"""P10-07C domain tests for immutable slot-authority records."""
import dataclasses
import json

import pytest

from position_identity import (
    AUTHORITY_SCHEMA_VERSION,
    AuthorityAck,
    AuthorityAckCode,
    AuthorityProvenance,
    AuthorityStatus,
    ExchangePositionKey,
    SlotAuthority,
)


def _key(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='binance-main',
        environment='PROD',
        symbol=symbol,
    )


def _active(*, episode='episode-A', generation=1, revision=2,
            provenance=AuthorityProvenance.NATIVE, alias=None):
    return SlotAuthority(
        exchange_position_key=_key(),
        episode_id=episode,
        slot_generation=generation,
        status=AuthorityStatus.ACTIVE,
        provenance=provenance,
        revision=revision,
        legacy_position_id_alias=alias,
        created_at=10,
        updated_at=20,
    )


def test_initial_flat_record_has_zero_high_water_and_revision_one():
    authority = SlotAuthority.initial_flat(_key(), now=10)
    assert authority.schema_version == AUTHORITY_SCHEMA_VERSION == 1
    assert authority.status is AuthorityStatus.FLAT
    assert authority.episode_id is None
    assert authority.provenance is None
    assert authority.slot_generation == 0
    assert authority.revision == 1


def test_allocate_episode_increments_generation_and_revision_separately():
    initial = SlotAuthority.initial_flat(_key(), now=10)
    active = initial.allocate_episode(
        episode_id='episode-A',
        provenance=AuthorityProvenance.NATIVE,
        status=AuthorityStatus.ACTIVE,
        legacy_position_id_alias=None,
        now=20,
    )
    assert active.slot_generation == 1
    assert active.revision == 2
    assert active.episode_id == 'episode-A'
    assert active.provenance is AuthorityProvenance.NATIVE


def test_flat_transition_retains_high_water_lineage_and_provenance():
    active = _active(alias='legacy-A')
    flat = active.transition_to_flat(now=30)
    assert flat.status is AuthorityStatus.FLAT
    assert flat.episode_id == 'episode-A'
    assert flat.provenance is AuthorityProvenance.NATIVE
    assert flat.legacy_position_id_alias == 'legacy-A'
    assert flat.slot_generation == 1
    assert flat.revision == 3


def test_reopen_uses_new_episode_and_next_generation():
    flat = _active().transition_to_flat(now=30)
    reopened = flat.allocate_episode(
        episode_id='episode-B',
        provenance=AuthorityProvenance.NATIVE,
        status=AuthorityStatus.ACTIVE,
        legacy_position_id_alias=None,
        now=40,
    )
    assert reopened.episode_id == 'episode-B'
    assert reopened.slot_generation == 2
    assert reopened.revision == 4


def test_record_round_trip_is_deterministic_and_field_order_independent():
    authority = _active(alias='legacy:BTC')
    encoded = authority.to_json()
    decoded = SlotAuthority.from_json(encoded)
    reordered = dict(reversed(list(authority.to_dict().items())))
    assert decoded == authority
    assert SlotAuthority.from_dict(reordered) == authority
    assert json.loads(encoded)['schema_version'] == 1


def test_unknown_schema_and_malformed_fields_are_rejected():
    payload = _active().to_dict()
    payload['schema_version'] = 2
    with pytest.raises(ValueError, match='unsupported authority schema'):
        SlotAuthority.from_dict(payload)
    payload = _active().to_dict()
    payload.pop('slot_generation')
    with pytest.raises(ValueError, match='invalid authority fields'):
        SlotAuthority.from_dict(payload)
    with pytest.raises(ValueError, match='not valid JSON'):
        SlotAuthority.from_json('{broken')


@pytest.mark.parametrize('schema_version', [True, 1.0])
def test_schema_version_requires_exact_integer(schema_version):
    payload = _active().to_dict()
    payload['schema_version'] = schema_version
    with pytest.raises(ValueError, match='unsupported authority schema'):
        SlotAuthority.from_dict(payload)


@pytest.mark.parametrize('field,value', [
    ('slot_generation', -1),
    ('slot_generation', True),
    ('revision', 0),
    ('revision', 1.5),
])
def test_generation_and_revision_validation(field, value):
    values = {
        'exchange_position_key': _key(),
        'episode_id': 'episode-A',
        'slot_generation': 1,
        'status': AuthorityStatus.ACTIVE,
        'provenance': AuthorityProvenance.NATIVE,
        'revision': 2,
        'legacy_position_id_alias': None,
        'created_at': 10,
        'updated_at': 20,
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        SlotAuthority(**values)


def test_active_and_quarantined_require_episode_and_provenance():
    for status in (AuthorityStatus.ACTIVE, AuthorityStatus.QUARANTINED):
        with pytest.raises(ValueError, match='requires episode_id'):
            SlotAuthority(
                exchange_position_key=_key(),
                episode_id=None,
                slot_generation=1,
                status=status,
                provenance=None,
                revision=2,
                legacy_position_id_alias=None,
                created_at=10,
                updated_at=20,
            )


def test_provenance_and_record_are_immutable_within_episode():
    authority = _active(provenance=AuthorityProvenance.MIGRATED)
    flat = authority.transition_to_flat(now=30)
    assert flat.provenance is AuthorityProvenance.MIGRATED
    with pytest.raises(dataclasses.FrozenInstanceError):
        authority.provenance = AuthorityProvenance.RECONSTRUCTED


def test_allocation_requires_flat_and_flat_transition_requires_active():
    with pytest.raises(ValueError, match='requires FLAT'):
        _active().allocate_episode(
            episode_id='episode-B',
            provenance=AuthorityProvenance.NATIVE,
            status=AuthorityStatus.ACTIVE,
            legacy_position_id_alias=None,
            now=30,
        )
    with pytest.raises(ValueError, match='requires ACTIVE'):
        SlotAuthority.initial_flat(_key(), now=10).transition_to_flat(now=20)


def test_typed_ack_exposes_exact_code_and_applied_state():
    applied = AuthorityAck(AuthorityAckCode.APPLIED, _active())
    conflict = AuthorityAck(AuthorityAckCode.CONFLICT, _active())
    assert applied.applied is True
    assert conflict.applied is False
    assert conflict.authority.slot_generation == 1
