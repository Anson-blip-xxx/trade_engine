"""P10-06A characterization of unfenced stale AlgoSL work."""
import copy
import threading

import pytest
import requests

from position_runtime import runtime as rt
from shared import position_manager as pm


class _StopWorker(Exception):
    pass


class _NoExchangeInfo:
    status_code = 503


def _run_one_task(monkeypatch):
    def stop_after_first_attempt(delay):
        assert delay == 11
        raise _StopWorker

    monkeypatch.setattr(rt.time, 'sleep', stop_after_first_attempt)
    with pytest.raises(_StopWorker):
        rt.algo_worker_loop(
            queue=pm._ALGO_QUEUE,
            lock=threading.Lock(),
            place_fn=pm._algo_place_sl_inner,
            log_fn=lambda message: None,
        )


def test_old_episode_task_cancels_creates_and_writes_into_reopened_episode(
        monkeypatch):
    """Episode A's four-tuple cannot distinguish same-symbol episode B."""
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    monkeypatch.setattr(pm, '_pmlog', lambda message: None)
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: _NoExchangeInfo())

    current = {
        'BTCUSDT': {
            'position_id': 'episode-A',
            'side': 'LONG',
            'algo_sl_id': 101,
        },
    }
    pm._algo_enqueue('BTCUSDT', 'SELL', 90.0, 2.0)

    # A closes and B reuses the exchange slot before A's queued work runs.
    current.pop('BTCUSDT')
    current['BTCUSDT'] = {
        'position_id': 'episode-B',
        'side': 'LONG',
        'algo_sl_id': 202,
    }

    events = []
    saved = []
    monkeypatch.setattr(
        pm, '_cancel_all_algo',
        lambda symbol: events.append(('cancel_all', symbol)),
    )

    def place(path, params=None):
        events.append(('place', path, dict(params or {})))
        return {'algoId': 303}

    monkeypatch.setattr(pm, '_light_fapi_post', place)
    monkeypatch.setattr(pm, '_load', lambda: current)
    monkeypatch.setattr(pm, '_save', lambda state: saved.append(copy.deepcopy(state)))

    _run_one_task(monkeypatch)

    assert pm._ALGO_QUEUE == []
    assert events[0] == ('cancel_all', 'BTCUSDT')
    assert events[1][0:2] == ('place', '/fapi/v1/algoOrder')
    assert events[1][2]['side'] == 'SELL'
    assert events[1][2]['triggerPrice'] == 90.0
    assert events[1][2]['quantity'] == 2.0
    assert saved[-1]['BTCUSDT']['position_id'] == 'episode-B'
    assert saved[-1]['BTCUSDT']['algo_sl_id'] == 303


def test_old_task_blindly_mutates_exchange_when_position_is_missing(
        monkeypatch):
    """Missing local position skips writeback, not cancel/create."""
    monkeypatch.setattr(pm, '_ALGO_QUEUE', [])
    monkeypatch.setattr(pm, '_pmlog', lambda message: None)
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: _NoExchangeInfo())
    pm._algo_enqueue('BTCUSDT', 'SELL', 90.0, 2.0)

    events = []
    saved = []
    monkeypatch.setattr(
        pm, '_cancel_all_algo',
        lambda symbol: events.append(('cancel_all', symbol)),
    )
    monkeypatch.setattr(
        pm, '_light_fapi_post',
        lambda path, params=None: events.append(('place', path)) or {
            'algoId': 404,
        },
    )
    monkeypatch.setattr(pm, '_load', lambda: {})
    monkeypatch.setattr(pm, '_save', lambda state: saved.append(state))

    _run_one_task(monkeypatch)

    assert events == [
        ('cancel_all', 'BTCUSDT'),
        ('place', '/fapi/v1/algoOrder'),
    ]
    assert saved == []
