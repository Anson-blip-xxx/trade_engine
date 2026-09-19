"""R2 durable desired-protection domain tests."""
from dataclasses import FrozenInstanceError, replace

import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.desired import (
    DesiredProtectionRecord,
    ProtectionStatus,
)

EPISODE = '6a4717d8-7390-4386-9878-56ff2981f93d'


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='r2-test', environment='SANDBOX',
        symbol='BTCUSDT')


def _record():
    return DesiredProtectionRecord.initial_pending(
        exchange_position_key=_slot(), episode_id=EPISODE,
        slot_generation=3, desired_intent_id='initial-stop',
        trigger_price=90, covered_quantity=2, closing_side='SELL',
        operation_id='declare-1', now=10)


def test_initial_record_is_frozen_versioned_and_round_trips():
    record = _record()
    assert record.protection_generation == 1
    assert record.revision == 1
    assert record.status is ProtectionStatus.PENDING
    assert DesiredProtectionRecord.from_json(record.to_json()) == record
    with pytest.raises(FrozenInstanceError):
        record.trigger_price = 91


def test_new_spec_advances_generation_and_revision_once():
    current = _record()
    next_record = current.next_desired(
        desired_intent_id='break-even', trigger_price=100,
        covered_quantity=2, closing_side='SELL',
        operation_id='declare-2', now=20)
    assert next_record.protection_generation == 2
    assert next_record.revision == 2
    assert next_record.status is ProtectionStatus.PENDING
    assert next_record.exchange_algo_aliases == ()


def test_unchanged_spec_or_reused_intent_cannot_advance_generation():
    current = _record()
    with pytest.raises(ValueError, match='unchanged'):
        current.next_desired(
            desired_intent_id='duplicate', trigger_price=90,
            covered_quantity=2, closing_side='SELL',
            operation_id='declare-2', now=20)
    with pytest.raises(ValueError, match='intent'):
        current.next_desired(
            desired_intent_id='initial-stop', trigger_price=91,
            covered_quantity=2, closing_side='SELL',
            operation_id='declare-2', now=20)


def test_status_transition_keeps_generation_and_advances_revision():
    current = _record()
    submitting = current.transition(
        status='SUBMITTING', operation_id='claim-1', now=20)
    unknown = submitting.transition(
        status='UNKNOWN', operation_id='ack-lost', now=21,
        exchange_algo_aliases=('algo-1',))
    assert unknown.protection_generation == 1
    assert unknown.revision == 3
    assert unknown.exchange_algo_aliases == ('algo-1',)


def test_terminal_and_illegal_transitions_fail_closed():
    current = _record()
    with pytest.raises(ValueError, match='illegal'):
        current.transition(status='ACTIVE', operation_id='bad', now=20)
    terminal = current.transition(
        status='FAILED', operation_id='failed', now=20)
    with pytest.raises(ValueError, match='illegal'):
        terminal.transition(status='PENDING', operation_id='retry', now=21)


@pytest.mark.parametrize('changes', [
    {'protection_generation': 0}, {'revision': 0},
    {'trigger_price': 0}, {'covered_quantity': 0},
    {'closing_side': 'LONG'}, {'status': 'PROTECTED'},
    {'exchange_algo_aliases': ['not-a-tuple']},
    {'exchange_algo_aliases': ('dup', 'dup')},
])
def test_invalid_records_are_rejected(changes):
    with pytest.raises((TypeError, ValueError)):
        replace(_record(), **changes)


def test_parser_requires_exact_fields():
    payload = _record().to_dict()
    payload['algo_sl_id'] = 'not-authority'
    with pytest.raises(ValueError, match='fields'):
        DesiredProtectionRecord.from_dict(payload)
