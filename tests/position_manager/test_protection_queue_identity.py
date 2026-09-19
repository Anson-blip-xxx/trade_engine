"""P10-D3D-1B immutable protection-queue identity contract."""
import threading
from dataclasses import FrozenInstanceError

import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.service import ProtectionService
from position_protection.task import (
    AlgoProtectionTask,
    QueueTaskClassification,
    classify_queue_task,
)
from position_runtime import runtime as rt
from shared import position_manager as pm

EPISODE_ID = '6a4717d8-7390-4386-9878-56ff2981f93d'


class _StopWorker(Exception):
    pass


def _slot(symbol='BTCUSDT'):
    return ExchangePositionKey.one_way(
        account_principal_id='trade-engine-demo',
        environment='DEMO',
        symbol=symbol,
    )


def _task(**overrides):
    values = {
        'symbol': 'BTCUSDT',
        'side': 'SELL',
        'trigger_price': 90.0,
        'qty': 2.0,
        'exchange_position_key': _slot(),
        'episode_id': EPISODE_ID,
        'slot_generation': 7,
        'protection_generation': 1,
    }
    values.update(overrides)
    return AlgoProtectionTask(**values)


def _run_one(monkeypatch, queue, execute_fn, logs):
    def stop_after_task(delay):
        assert delay == 11
        raise _StopWorker

    monkeypatch.setattr(rt.time, 'sleep', stop_after_task)
    with pytest.raises(_StopWorker):
        rt.algo_worker_loop(
            queue=queue,
            lock=threading.Lock(),
            execute_fn=execute_fn,
            log_fn=logs.append,
        )


def test_task_is_frozen_and_carries_complete_identity():
    task = _task()
    assert task.exchange_position_key == _slot()
    assert task.episode_id == EPISODE_ID
    assert task.slot_generation == 7
    assert task.protection_generation == 1
    with pytest.raises(FrozenInstanceError):
        task.qty = 3.0


@pytest.mark.parametrize('field,value', [
    ('side', 'LONG'),
    ('trigger_price', 0),
    ('qty', float('nan')),
    ('episode_id', 'legacy-position-id'),
    ('slot_generation', 0),
    ('protection_generation', 0),
])
def test_task_rejects_malformed_or_incomplete_identity(field, value):
    with pytest.raises((TypeError, ValueError)):
        _task(**{field: value})


def test_task_symbol_must_match_canonical_slot():
    with pytest.raises(ValueError, match='symbol'):
        _task(exchange_position_key=_slot('ETHUSDT'))


def test_classifier_distinguishes_fenced_legacy_and_malformed():
    task = _task()
    assert classify_queue_task(task).classification \
        is QueueTaskClassification.FENCED
    assert classify_queue_task((
        'BTCUSDT', 'SELL', 90.0, 2.0,
    )).classification is QueueTaskClassification.LEGACY_UNFENCED
    assert classify_queue_task({'symbol': 'BTCUSDT'}).classification \
        is QueueTaskClassification.MALFORMED


def test_pm_enqueue_builds_immutable_task_when_identity_is_complete(
        monkeypatch):
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    pm._algo_enqueue(
        'BTCUSDT', 'SELL', 90.0, 2.0,
        exchange_position_key=_slot(), episode_id=EPISODE_ID,
        slot_generation=7, protection_generation=1,
    )
    assert pm._ALGO_QUEUE == [_task()]


def test_pm_enqueue_rejects_partial_identity_without_queueing(monkeypatch):
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    with pytest.raises(ValueError, match='all be supplied'):
        pm._algo_enqueue(
            'BTCUSDT', 'SELL', 90.0, 2.0, episode_id=EPISODE_ID,
        )
    assert pm._ALGO_QUEUE == []


def test_worker_delegates_fenced_shape_to_authorized_executor(monkeypatch):
    executed = []
    logs = []
    _run_one(
        monkeypatch, [_task()],
        executed.append, logs,
    )
    assert executed == [_task()]
    assert logs == []


@pytest.mark.parametrize('value,reason', [
    (('BTCUSDT', 'SELL', 90.0, 2.0), 'LEGACY_UNFENCED'),
    ({'symbol': 'BTCUSDT'}, 'MALFORMED'),
])
def test_worker_drops_unfenced_or_malformed_before_place(
        monkeypatch, value, reason):
    placed = []
    logs = []
    _run_one(
        monkeypatch, [value],
        lambda *args: placed.append(args), logs,
    )
    assert placed == []
    assert len(logs) == 1
    assert f'reason={reason}' in logs[0]


def test_protection_service_forwards_complete_identity():
    calls = []
    service = ProtectionService(
        enqueue_fn=lambda *args, **kwargs: calls.append((args, kwargs)),
        start_worker_fn=lambda: None,
        place_algo_sl_fn=lambda *args: {},
        cancel_id_fn=lambda value: {},
        cancel_all_fn=lambda symbol: None,
    )
    service.enqueue_algo_sl(
        'BTCUSDT', 'SELL', 90.0, 2.0,
        exchange_position_key=_slot(), episode_id=EPISODE_ID,
        slot_generation=7, protection_generation=1,
    )
    assert calls == [(('BTCUSDT', 'SELL', 90.0, 2.0), {
        'exchange_position_key': _slot(),
        'episode_id': EPISODE_ID,
        'slot_generation': 7,
        'protection_generation': 1,
    })]
