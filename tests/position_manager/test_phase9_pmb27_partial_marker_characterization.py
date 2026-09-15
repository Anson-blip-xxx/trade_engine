"""P9-04A：PMB-27 characterization（partial close marker interaction）。"""
from __future__ import annotations

import pytest

import shared.position_manager as pm


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0, 'sl': 1.5}
    pos.update(over)
    return pos


class Snapshot:
    def __init__(self):
        self.mc, self.clr, self.wcr = [], [], []


@pytest.fixture
def menv(monkeypatch, fake_redis):
    s = Snapshot()
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))
    s.mc = m = []
    monkeypatch.setattr(pm, '_mark_closed', lambda s2: m.append(s2))
    monkeypatch.setattr(pm, '_clear_closed_marker', lambda s2: m.append(
        (s2, 'clear')))
    monkeypatch.setattr(pm, '_was_closed_recently',
                        lambda s, within_hours=4:
                        ('TUSDT' if False else (s, 'clear')) in m
                        or s in [] and s not in m) if False else None

    def wcr(sym, within_hours=4):
        return any(x == sym for x in m)
    monkeypatch.setattr(pm, '_was_closed_recently', wcr)
    return {'pm': pm, 'mc': m}


def _close_env(monkeypatch, risk_pages, executed='4'):
    """执行 env：risk pages + exec 结果。"""
    reads = {'n': 0}
    monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: None)

    def fapi_get(path, params=None):
        reads['n'] += 1
        return risk_pages[min(reads['n'] - 1, len(risk_pages) - 1)]
    state = {}
    monkeypatch.setattr(pm, '_save', lambda p: state.update(p))
    monkeypatch.setattr(pm._exec_core, 'close_intent',
                        staticmethod(
                lambda s, side, q: ('ci', s, side, q)))

    def exec_order(intent):
        class R:
            raw = {'orderId': 9, 'status': 'FILLED',
                   'executedQty': executed_v['v']}
        return R()
    executed_v = {'v': '4'}
    monkeypatch.setattr(pm, '_s6api', lambda: (
        lambda p, params=None: risk_pages[min(reads['n'] - 1, len(risk_pages) - 1)],
        lambda p, q=None: {'orderId': 9}, None, None, None, None, None,
        lambda *a, **k: None))

    def execute(intent):
        class R:
            raw = {'orderId': 9, 'status': 'FILLED',
                   'executedQty': executed_v['v']}
        return R()
    monkeypatch.setattr(pm, '_execution_service',
                        lambda: type('X', (), {
                            'execute_order': staticmethod(execute)})())
    return pm, reads, state, executed_v


class TestMarkerByOutcome:
    def test_full_success_keeps_marker(self, menv, monkeypatch):
        pm, reads, state, ev = _close_env(monkeypatch, risk_pages=[
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}],
            []])
        ev['v'] = '10'
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: None)
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok is True
        assert positions == {}
        assert menv['mc'] == ['AUSDT']             # 成功后 marker keep

    def test_partial_fill_keeps_marker_pmb27(self, menv, monkeypatch):
        """PMB-27 root：partial fill close → marker keep（不 clear）。"""
        pm, reads, state, ev = _close_env(monkeypatch, risk_pages=[
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}],
            [{'symbol': 'AUSDT', 'positionAmt': '-6'}],
            [{'symbol': 'AUSDT', 'positionAmt': '-6'}]])
        ev['v'] = '4'
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions, force=True)
        assert ok is False
        assert pos['qty'] == 6.0
        assert menv['mc'] == ['AUSDT']                  # mark 已设
        # PMB-27 key assertion：没有 (sym, 'clear') 记录 —— marker 保留
        assert menv['mc'].count(('AUSDT', 'clear')) == 0

    def test_no_fill_keeps_marker(self, menv, monkeypatch):
        pm, reads, state, ev = _close_env(monkeypatch, risk_pages=[
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}],
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}],
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}]])
        ev['v'] = '0.0'
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions, force=True)
        assert ok is False and pos['qty'] == 10.0
        assert menv['mc'].count(('AUSDT', 'clear')) == 0   # keep（PMB-27）

    def test_execution_fail_clears_marker(self, menv, monkeypatch):
        """exec code fail → clear marker（对照 path）。"""
        pm, reads, state, ev = _close_env(monkeypatch, risk_pages=[
            [{'symbol': 'AUSDT', 'positionAmt': '-10'}]])
        pos = _pos()
        positions = {'AUSDT': pos}

        def exec_order(intent):
            class R:
                raw = {'code': -2019, 'msg': 'bad'}
            return R()
        monkeypatch.setattr(pm, '_execution_service', lambda: type('X', (), {
            'execute_order': staticmethod(exec_order)})())
        monkeypatch.setattr(pm, '_log_close_error',
                            lambda s, m, interval=60: None)
        ok = pm._close('AUSDT', pos, 2.1, '紧急止损', positions,
                       force=True)
        assert ok is False
        assert ('AUSDT', 'clear') in menv['mc']       # failure path clear


class TestSecondCloseBlock:
    def test_second_nonforce_close_blocked_by_marker_pmb27(self, menv,
                                                           monkeypatch):
        pm = menv['pm']
        # simulate partial-fill path残留 marker
        m_append = menv['mc']
        m_append.append('AUSDT')  # marker 在

        def wcr(sym, within_hours=4):
            return any(x == sym for x in m_append)
        monkeypatch.setattr(pm, '_was_closed_recently', wcr)
        pos = _pos(qty=6.0)
        positions = {'AUSDT': pos}
        # 第二次 non-force close：被 guard 阻断（PMB-27 reproduction）
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok is False
        # force bypass
        ok2 = pm._close('AUSDT', pos, 2.1, '硬止损', positions, force=True)
        # force 路径独立 marker handoff（不阻断）——但不验证 order 结果，
        # 只验证 marker guard skip
        assert m_append.count('AUSDT') == 2           # force 不被 guard 拦


class TestPartialCloseNoMarker:
    def test_partial_close_route_has_no_marker_interaction(self, menv):
        """策略级 `_partial_close` 从不 touch marker（对照 path）。"""
        import inspect
        src = inspect.getsource(menv['pm']._lifecycle_service.__class__ if False
                                else __import__('position_lifecycle.service',
                                                fromlist=['x']).PositionLifecycleService.partial_close)
        assert 'self.state.mc' not in src and 'mark_closed' not in src
        assert 'clr' not in src.split('def partial_close')[1]
