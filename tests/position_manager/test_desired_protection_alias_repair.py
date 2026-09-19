"""R2 same-status revision is reserved for actual alias repair."""
import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.desired import DesiredProtectionRecord


def _submitting():
    pending = DesiredProtectionRecord.initial_pending(
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id='alias-repair-test', environment='SANDBOX',
            symbol='BTCUSDT'),
        episode_id='6a4717d8-7390-4386-9878-56ff2981f93d',
        slot_generation=1, desired_intent_id='initial-stop',
        trigger_price=90, covered_quantity=1, closing_side='SELL',
        operation_id='declare-1', now=1)
    return pending.transition(
        status='SUBMITTING', operation_id='claim-1', now=2)


def test_same_status_requires_explicit_real_alias_change():
    current = _submitting()
    with pytest.raises(ValueError, match='alias repair'):
        current.transition(
            status='SUBMITTING', operation_id='noop', now=3,
            allow_same_status_alias_repair=True)
    with pytest.raises(ValueError, match='alias repair'):
        current.transition(
            status='SUBMITTING', operation_id='implicit', now=3,
            exchange_algo_aliases=('algo-1',))

    repaired = current.transition(
        status='SUBMITTING', operation_id='repair', now=3,
        exchange_algo_aliases=('algo-1',),
        allow_same_status_alias_repair=True)
    assert repaired.exchange_algo_aliases == ('algo-1',)
    assert repaired.protection_generation == current.protection_generation
    assert repaired.revision == current.revision + 1
