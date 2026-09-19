"""R1 episode identity fields cannot drift during revision."""
import pytest

from position_identity.projection import LivePositionProjection
from position_identity.slot import ExchangePositionKey


def _projection():
    return LivePositionProjection(
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id='immutable-test', environment='SANDBOX',
            symbol='BTCUSDT'),
        episode_id='6a4717d8-7390-4386-9878-56ff2981f93d',
        slot_generation=1, identity_provenance='MIGRATED',
        state_revision=1, last_operation_id='migration-1',
        side='LONG', system='S6', quantity=1, entry_price=100,
        opened_at=1, updated_at=1,
        legacy_position_id_alias='S6:BTCUSDT:legacy',
    )


@pytest.mark.parametrize('field,value', [
    ('exchange_position_key', None),
    ('episode_id', '887246d6-b2e6-4e04-8308-bc3a3577282c'),
    ('slot_generation', 2),
    ('identity_provenance', 'NATIVE'),
    ('schema_version', 1),
    ('state_revision', 2),
    ('last_operation_id', 'forged'),
    ('opened_at', 2),
    ('side', 'SHORT'),
    ('system', 'S8'),
    ('legacy_position_id_alias', 'other'),
])
def test_revise_rejects_episode_identity_changes(field, value):
    with pytest.raises(ValueError, match='immutable'):
        _projection().revise(
            operation_id='update-2', now=2, **{field: value})
