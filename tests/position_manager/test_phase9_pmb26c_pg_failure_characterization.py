"""P9-09A: PMB-26C PostgreSQL failure characterization."""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

import shared.position_manager as pm
import shared.postgres_client as pg_client
from position_reconcile import deps as rcd
from position_reconcile.service import PositionReconcileService


def _raw(symbol=None):
    position = {
        'entry': 100.0,
        'qty': 2.0,
        'side': 'LONG',
        'leverage': 5,
        'margin': 'CROSSED',
    }
    return {symbol: position} if symbol else position


def _build(*, pg_result=None, pg_error=None, tg_error=None):
    calls = {'pg': [], 'tg': [], 'logs': [], 'order': []}
    store = {}
    clock = {'now': 1000.0}

    class Transport:
        def post(self, *args, **kwargs):
            calls['tg'].append((args, kwargs))
            calls['order'].append('tg')
            if tg_error is not None:
                raise tg_error
            return True

    def record_event(event):
        calls['pg'].append(event)
        calls['order'].append('pg')
        if pg_error is not None:
            raise pg_error
        return pg_result

    def redis_set(key, value):
        store[key] = value
        kind = 'pending' if ':pending:' in key else 'seen'
        calls['order'].append(kind)

    def log(message):
        calls['logs'].append(str(message))
        calls['order'].append('log')

    service = PositionReconcileService(
        runtime=rcd.ReconcileRuntimeDeps(
            lgt=log,
            now=lambda: clock['now'],
        ),
        state=rcd.ReconcileStateDeps(
            load=lambda: {},
            save=lambda positions: None,
            wcr=lambda symbol: False,
            mc=lambda symbol: None,
            posid=lambda symbol, position: 'PID',
            rq=lambda symbol, qty: qty,
        ),
        coordination=rcd.ReconcileCoordinationDeps(
            lacq=lambda key, owner, ttl=45: True,
            lrel=lambda key, owner: None,
            rdget=lambda key: store.get(key),
            rdset=redis_set,
            pid=lambda: 1,
            uid=lambda: uuid.uuid4().hex[:8],
        ),
        notification=rcd.ReconcileNotificationDeps(
            pg=record_event,
            tgt='TOKEN',
            tgc='CHAT',
            rqst=Transport(),
        ),
        action=rcd.ReconcileActionDeps(
            exf=lambda path, params=None: [],
            gpx=lambda symbol: None,
            s6=lambda: (None,) * 8,
            sandbox=lambda: False,
            sk=None,
        ),
    )
    return service, calls, store, clock


def _trigger(service, clock, symbol='TUSDT'):
    service.notify_external_position(symbol, _raw(), 'S6')
    clock['now'] += 30
    return service.notify_external_position(symbol, _raw(), 'S6')


class TestPgSuccessAndReturnContract:
    def test_success_called_once_with_external_position_payload(self):
        service, calls, _, clock = _build(pg_result=True)

        result = _trigger(service, clock)

        assert result is None
        assert len(calls['pg']) == 1
        assert calls['pg'][0] == {
            'event_id': 'external:TUSDT:LONG:100:2',
            'position_id': 'external:TUSDT:LONG:100:2',
            'event_type': 'EXTERNAL_POSITION_DETECTED',
            'order_id': '',
            'fill_id': '',
            'price': 100.0,
            'qty': 2.0,
            'realized_pnl': 0.0,
            'payload': {'system': 'S6', 'raw': _raw()},
        }

    @pytest.mark.parametrize(
        'pg_result', [None, False, object()], ids=['none', 'false', 'object'])
    def test_return_value_is_ignored(self, pg_result):
        service, calls, store, clock = _build(pg_result=pg_result)

        result = _trigger(service, clock)

        assert result is None
        assert len(calls['pg']) == 1
        assert store['alert:external_position:TUSDT']['fingerprint'] == \
            'LONG:100:2'

    def test_success_order_is_seen_pending_log_tg_pg(self):
        service, calls, _, clock = _build(pg_result=True)

        _trigger(service, clock)

        assert calls['order'] == [
            'pending', 'seen', 'pending', 'log', 'tg', 'pg']


class TestConcretePgHelper:
    def test_connection_exception_becomes_false(self, monkeypatch):
        @contextmanager
        def broken_connection():
            raise RuntimeError('connection failed')
            yield

        monkeypatch.setattr(pg_client, 'enabled', lambda: True)
        monkeypatch.setattr(pg_client, '_connection', broken_connection)

        assert pg_client.record_trade_event({'payload': {}}) is False

    def test_pre_try_serialization_exception_propagates(self, monkeypatch):
        payload = {}
        payload['cycle'] = payload
        monkeypatch.setattr(pg_client, 'enabled', lambda: True)

        with pytest.raises(ValueError, match='Circular reference'):
            pg_client.record_trade_event({'payload': payload})


class TestRaisedPgException:
    def test_exception_propagates_after_tg_and_alert_state(self):
        service, calls, store, clock = _build(
            pg_error=RuntimeError('pg unavailable'))

        with pytest.raises(RuntimeError, match='pg unavailable'):
            _trigger(service, clock)

        assert calls['order'] == [
            'pending', 'seen', 'pending', 'log', 'tg', 'pg']
        assert store['alert:external_position:TUSDT'] == {
            'fingerprint': 'LONG:100:2', 'ts': 1030.0}
        assert store['alert:external_position:pending:TUSDT'] == {}

    def test_exception_has_no_pg_specific_log(self):
        service, calls, _, clock = _build(pg_error=RuntimeError('pg'))

        with pytest.raises(RuntimeError, match='pg'):
            _trigger(service, clock)

        assert calls['logs'] == [
            '[外部仓位] TUSDT LONG entry=100.0 qty=2.0 '
            '未找到本地开仓事件']

    def test_tg_exception_remains_swallowed_before_pg_propagates(self):
        service, calls, _, clock = _build(
            tg_error=RuntimeError('tg'), pg_error=RuntimeError('pg'))

        with pytest.raises(RuntimeError, match='pg'):
            _trigger(service, clock)

        assert len(calls['tg']) == 1
        assert len(calls['pg']) == 1


class TestRetryAfterRaisedPg:
    def test_next_cycle_reenters_grace_then_seen_suppresses_pg(self):
        service, calls, store, clock = _build(pg_error=RuntimeError('pg'))
        with pytest.raises(RuntimeError):
            _trigger(service, clock)

        service.notify_external_position('TUSDT', _raw(), 'S6')
        assert store['alert:external_position:pending:TUSDT'] == {
            'fingerprint': 'LONG:100:2', 'ts': 1030.0}
        clock['now'] += 30
        service.notify_external_position('TUSDT', _raw(), 'S6')

        assert len(calls['pg']) == 1
        assert len(calls['tg']) == 1

    def test_seen_expiry_allows_pg_retry(self):
        service, calls, _, clock = _build(pg_error=RuntimeError('pg'))
        with pytest.raises(RuntimeError):
            _trigger(service, clock)
        service.notify_external_position('TUSDT', _raw(), 'S6')
        clock['now'] += 86400

        with pytest.raises(RuntimeError, match='pg'):
            service.notify_external_position('TUSDT', _raw(), 'S6')

        assert len(calls['pg']) == 2
        assert len(calls['tg']) == 2


@pytest.fixture
def merge_env(monkeypatch, fake_redis):
    calls = {'clear': [], 'pg': [], 'tg': [], 'logs': [], 'order': []}
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr(pm, '_pmlog',
                        lambda message: calls['logs'].append(str(message)))
    monkeypatch.setattr(pm.time, 'time', lambda: 1000.0)
    monkeypatch.setattr(pm, '_TG_TOKEN', 'TOKEN')
    monkeypatch.setattr(pm, '_TG_CHAT_ID', 'CHAT')
    def tg_post(*args, **kwargs):
        calls['tg'].append((args, kwargs))
        calls['order'].append('tg')
        return True

    monkeypatch.setattr(pm.requests, 'post', tg_post)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda symbol, window=4: True)
    def clear_marker(symbol):
        calls['clear'].append(symbol)
        calls['order'].append(f'clear:{symbol}')

    monkeypatch.setattr(pm, '_clear_closed_marker', clear_marker)
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


def _prime_pending(redis, *symbols):
    for symbol in symbols:
        redis.set(f'alert:external_position:pending:{symbol}', {
            'fingerprint': 'LONG:100:2', 'ts': 970.0})


class TestMergeFailureAtomicity:
    def test_marker_clear_survives_pg_failure_but_state_save_does_not(self,
                                                                     merge_env,
                                                                     monkeypatch):
        env = merge_env
        initial = {'KEEPUSDT': {'entry': 50.0, 'qty': 1.0, 'side': 'LONG'}}
        env['redis'].set('pm:positions', initial)
        _prime_pending(env['redis'], 'AUSDT')

        def pg_boom(event):
            env['calls']['pg'].append(event)
            env['calls']['order'].append('pg:AUSDT')
            raise RuntimeError('pg')

        monkeypatch.setattr(pm, '_pg_record_event', pg_boom)

        with pytest.raises(RuntimeError, match='pg'):
            pm._merge_and_save(_raw('AUSDT'), 1000.0)

        assert env['calls']['clear'] == ['AUSDT']
        assert env['calls']['order'] == [
            'clear:AUSDT', 'tg', 'pg:AUSDT']
        assert env['redis'].get('pm:positions') == initial
        assert env['redis'].get(
            'alert:external_position:AUSDT')['fingerprint'] == 'LONG:100:2'
        assert env['redis'].get(
            'alert:external_position:pending:AUSDT') == {}

    def test_a_success_b_raise_skips_c_and_aborts_snapshot_save(self,
                                                                merge_env,
                                                                monkeypatch):
        env = merge_env
        initial = {'KEEPUSDT': {'entry': 50.0, 'qty': 1.0, 'side': 'LONG'}}
        env['redis'].set('pm:positions', initial)
        _prime_pending(env['redis'], 'AUSDT', 'BUSDT', 'CUSDT')
        raw = {}
        raw.update(_raw('AUSDT'))
        raw.update(_raw('BUSDT'))
        raw.update(_raw('CUSDT'))

        def pg(event):
            symbol = event['event_id'].split(':')[1]
            env['calls']['pg'].append(symbol)
            env['calls']['order'].append(f'pg:{symbol}')
            if symbol == 'BUSDT':
                raise RuntimeError('B failed')
            return True

        monkeypatch.setattr(pm, '_pg_record_event', pg)

        with pytest.raises(RuntimeError, match='B failed'):
            pm._merge_and_save(raw, 1000.0)

        assert env['calls']['pg'] == ['AUSDT', 'BUSDT']
        assert env['calls']['clear'] == ['AUSDT', 'BUSDT']
        assert len(env['calls']['tg']) == 2
        assert env['redis'].get('alert:external_position:CUSDT') is None
        assert env['redis'].get('pm:positions') == initial

    def test_false_from_wired_helper_allows_batch_and_save(self, merge_env,
                                                            monkeypatch):
        env = merge_env
        env['redis'].set('pm:positions', {})
        _prime_pending(env['redis'], 'AUSDT', 'BUSDT', 'CUSDT')
        raw = {}
        for symbol in ('AUSDT', 'BUSDT', 'CUSDT'):
            raw.update(_raw(symbol))

        def pg_false(event):
            symbol = event['event_id'].split(':')[1]
            env['calls']['pg'].append(symbol)
            env['calls']['order'].append(f'pg:{symbol}')
            return False

        monkeypatch.setattr(pm, '_pg_record_event', pg_false)

        merged = pm._merge_and_save(raw, 1000.0)

        assert env['calls']['pg'] == ['AUSDT', 'BUSDT', 'CUSDT']
        assert list(merged) == ['AUSDT', 'BUSDT', 'CUSDT']
        assert list(env['redis'].get('pm:positions')) == [
            'AUSDT', 'BUSDT', 'CUSDT']
