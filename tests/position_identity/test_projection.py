"""R1 canonical live-position projection domain tests."""
from dataclasses import FrozenInstanceError

import pytest

from position_identity.authority import AuthorityProvenance
from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey

EPISODE = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo', environment='DEMO',
        symbol='BTCUSDT')


def _projection(**overrides):
    values = {
        'exchange_position_key': _slot(), 'episode_id': EPISODE,
        'slot_generation': 3, 'identity_provenance': 'NATIVE',
        'state_revision': 1, 'last_operation_id': 'open-1',
        'side': 'LONG', 'system': 'S6', 'quantity': 2,
        'entry_price': 100, 'opened_at': 10, 'updated_at': 10,
        'legacy_position_id_alias': None,
    }
    values.update(overrides)
    return LivePositionProjection(**values)


def test_projection_round_trip_is_frozen_and_canonical():
    projection = _projection()
    assert LivePositionProjection.from_json(projection.to_json()) == projection
    assert projection.identity_provenance is AuthorityProvenance.NATIVE
    with pytest.raises(FrozenInstanceError):
        projection.quantity = 3


@pytest.mark.parametrize('field,value', [
    ('episode_id', 'legacy-id'), ('slot_generation', 0),
    ('state_revision', 0), ('last_operation_id', ''), ('side', 'BUY'),
    ('quantity', 0), ('entry_price', float('nan')),
    ('updated_at', 9), ('identity_provenance', 'UNKNOWN'),
])
def test_projection_rejects_invalid_identity_or_live_values(field, value):
    with pytest.raises((TypeError, ValueError)):
        _projection(**{field: value})


def test_revise_advances_only_revision_and_mutable_values():
    current = _projection()
    revised = current.revise(
        operation_id='reconcile-2', now=20, quantity=3)
    assert revised.state_revision == 2
    assert revised.last_operation_id == 'reconcile-2'
    assert revised.quantity == 3
    assert revised.episode_id == current.episode_id
    with pytest.raises(ValueError, match='immutable'):
        current.revise(operation_id='bad', now=20, episode_id=EPISODE)


def test_projection_requires_exact_schema_fields():
    payload = _projection().to_dict()
    payload['unexpected'] = True
    with pytest.raises(ValueError, match='fields'):
        LivePositionProjection.from_dict(payload)
