"""P9-08A: PMB-26B Telegram failure characterization."""
from __future__ import annotations

import uuid

import pytest

from position_reconcile import deps as rcd
from position_reconcile.service import PositionReconcileService


def _raw(side='LONG'):
    return {'entry': 100.0, 'qty': 2.0, 'side': side}


def _build(*, tg_result=None, tg_error=None, pg=None):
    calls = {'tg': [], 'pg': [], 'logs': [], 'order': []}
    store = {}
    clock = {'now': 1000.0}

    class Transport:
        def post(self, *args, **kwargs):
            calls['tg'].append((args, kwargs))
            calls['order'].append('tg')
            if tg_error is not None:
                raise tg_error
            return tg_result

    def record_event(event):
        calls['pg'].append(event)
        calls['order'].append('pg')
        if pg is not None:
            return pg(event)
        return None

    def redis_set(key, value):
        store[key] = value

    service = PositionReconcileService(
        runtime=rcd.ReconcileRuntimeDeps(
            lgt=lambda message: calls['logs'].append(str(message)),
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
    service.notify_external_position(symbol, _raw(), 'S6')


class TestTelegramFailureTopology:
    def test_exception_is_swallowed_and_pg_still_fires(self):
        service, calls, store, clock = _build(
            tg_error=RuntimeError('telegram unavailable'))

        _trigger(service, clock)

        assert len(calls['tg']) == 1
        assert len(calls['pg']) == 1
        assert calls['order'] == ['tg', 'pg']
        assert len(calls['logs']) == 1
        assert store['alert:external_position:TUSDT'] == {
            'fingerprint': 'LONG:100:2', 'ts': 1030.0}
        assert store['alert:external_position:pending:TUSDT'] == {}

    @pytest.mark.parametrize('tg_result', [None, False, True])
    def test_transport_return_value_does_not_gate_pg(self, tg_result):
        service, calls, _, clock = _build(tg_result=tg_result)

        _trigger(service, clock)

        assert len(calls['tg']) == 1
        assert len(calls['pg']) == 1

    def test_failure_on_one_symbol_does_not_block_the_next(self):
        service, calls, _, clock = _build(tg_error=RuntimeError('tg'))
        service.notify_external_position('AUSDT', _raw(), 'S6')
        service.notify_external_position('BUSDT', _raw(), 'S6')
        clock['now'] += 30

        service.notify_external_position('AUSDT', _raw(), 'S6')
        service.notify_external_position('BUSDT', _raw(), 'S6')

        assert len(calls['tg']) == 2
        assert len(calls['pg']) == 2


class TestSeenRetrySemantics:
    def test_failed_delivery_is_seen_and_suppressed_for_24_hours(self):
        service, calls, store, clock = _build(tg_error=RuntimeError('tg'))
        _trigger(service, clock)

        service.notify_external_position('TUSDT', _raw(), 'S6')
        clock['now'] += 30
        service.notify_external_position('TUSDT', _raw(), 'S6')

        assert len(calls['tg']) == 1
        assert len(calls['pg']) == 1
        assert store['alert:external_position:TUSDT']['ts'] == 1030.0
        assert store['alert:external_position:pending:TUSDT'][
            'fingerprint'] == 'LONG:100:2'

    def test_seen_expiry_allows_another_delivery_attempt(self):
        service, calls, _, clock = _build(tg_error=RuntimeError('tg'))
        _trigger(service, clock)
        service.notify_external_position('TUSDT', _raw(), 'S6')
        clock['now'] += 86400

        service.notify_external_position('TUSDT', _raw(), 'S6')

        assert len(calls['tg']) == 2
        assert len(calls['pg']) == 2


class TestPgSeparation:
    def test_pg_exception_propagates_after_tg_and_state_writes(self):
        def pg_boom(event):
            raise RuntimeError('pg unavailable')

        service, calls, store, clock = _build(pg=pg_boom)
        service.notify_external_position('TUSDT', _raw(), 'S6')
        clock['now'] += 30

        with pytest.raises(RuntimeError, match='pg unavailable'):
            service.notify_external_position('TUSDT', _raw(), 'S6')

        assert calls['order'] == ['tg', 'pg']
        assert store['alert:external_position:TUSDT']['fingerprint'] == \
            'LONG:100:2'
        assert store['alert:external_position:pending:TUSDT'] == {}
