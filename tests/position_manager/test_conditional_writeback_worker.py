"""R3 V3 queue identity and no-legacy-fallback worker tests."""
from position_identity import ExchangePositionKey
from position_protection.task import (
    ConditionalWritebackProtectionTask,
    QueueTaskClassification,
    classify_queue_task,
)
from position_protection.writeback import WritebackCode, WritebackResult
from shared import position_manager as pm

EPISODE = '6a4717d8-7390-4386-9878-56ff2981f93d'


class _NoExchangeInfo:
    status_code = 503


def _slot():
    return ExchangePositionKey.one_way(
        account_principal_id='r3-worker', environment='SANDBOX',
        symbol='BTCUSDT')


def test_v3_task_is_immutable_fenced_identity():
    task = ConditionalWritebackProtectionTask(
        symbol='BTCUSDT', side='SELL', trigger_price=90, qty=2,
        exchange_position_key=_slot(), episode_id=EPISODE,
        slot_generation=3, protection_generation=4,
        desired_revision=5, projection_revision=6)
    parsed = classify_queue_task(task)
    assert parsed.classification is QueueTaskClassification.FENCED
    assert parsed.task is task
    assert task.desired_revision == 5
    assert task.projection_revision == 6


def test_enqueue_requires_both_v3_revisions(monkeypatch):
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    monkeypatch.setattr(pm, '_pmlog', lambda _message: None)
    try:
        pm._algo_enqueue(
            'BTCUSDT', 'SELL', 90, 2,
            exchange_position_key=_slot(), episode_id=EPISODE,
            slot_generation=3, protection_generation=4,
            desired_revision=5)
    except ValueError as exc:
        assert 'both' in str(exc)
    else:
        raise AssertionError('partial V3 identity was accepted')

    pm._algo_enqueue(
        'BTCUSDT', 'SELL', 90, 2,
        exchange_position_key=_slot(), episode_id=EPISODE,
        slot_generation=3, protection_generation=4,
        desired_revision=5, projection_revision=6)
    assert isinstance(
        pm._ALGO_QUEUE[0], ConditionalWritebackProtectionTask)


def test_fenced_ack_never_falls_back_to_symbol_snapshot(monkeypatch):
    monkeypatch.setattr(pm.requests, 'get', lambda *_a, **_k: _NoExchangeInfo())
    monkeypatch.setattr(pm, '_cancel_all_algo', lambda _symbol: None)
    monkeypatch.setattr(
        pm, '_light_fapi_post', lambda *_a, **_k: {'algoId': 77})
    saves = []
    monkeypatch.setattr(pm, '_load', lambda: {'BTCUSDT': {'algo_sl_id': 0}})
    monkeypatch.setattr(pm, '_save', lambda value: saves.append(value))
    callbacks = []

    def stale(alias):
        callbacks.append(alias)
        return WritebackResult(WritebackCode.STALE)

    result = pm._algo_place_sl_inner(
        'BTCUSDT', 'SELL', 90, 2, before_create=lambda: True,
        ack_writeback=stale, legacy_writeback=False)
    assert result == {'algoId': 77}
    assert callbacks == [77]
    assert saves == []
