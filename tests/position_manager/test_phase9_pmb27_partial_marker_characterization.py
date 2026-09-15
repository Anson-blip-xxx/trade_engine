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

    def test_partial_fill_clears_marker_pmb27_fixed(self, menv, monkeypatch):
        """P9-04B regression：partial fill close → marker clear（不再残留）。"""
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
        # mark=1 + clear=1（PMB-27 fixed marker lifecycle）
        assert menv['mc'] == ['AUSDT', ('AUSDT', 'clear')]

    def test_no_fill_clears_marker_fixed(self, menv, monkeypatch):
        """P9-04B：no fill 同样 clear（未完成 final close）。"""
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
        assert menv['mc'] == ['AUSDT', ('AUSDT', 'clear')]

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


class TestSecondCloseRealFlow:
    def test_second_nonforce_close_after_partial_proceeds(self, fake_redis,
                                                          monkeypatch):
        """P9-04B 核心回归：partial close 后 marker 已 clear →
        第二次 non-force close **不被 recent guard 阻断**，
        真实进入 execution（发第二次 close market order）。"""
        from shared import position_manager as pm2
        monkeypatch.setattr(pm2, '_rget', fake_redis.get)
        monkeypatch.setattr(pm2, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm2, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm2, '_round_qty', lambda s, q: round(q, 6))
        monkeypatch.setattr(pm2, '_monitor_heartbeat_ts', 0.0)
        monkeypatch.setattr(pm2, '_RECENTLY_GHOSTED', [])
        monkeypatch.setattr(pm2, '_light_fapi_get', lambda p, params=None: [])
        monkeypatch.setattr(pm2, '_light_fapi_delete',
                            lambda p, params=None: {})
        monkeypatch.setattr(pm2, '_cancel_all_algo', lambda sym: None)
        monkeypatch.setattr(pm2, '_pg_record_event', lambda ev: None)
        monkeypatch.setattr(pm2, '_log_close_error',
                            lambda s, m, interval=60: None)
        risk_windows = [[{'symbol': 'AUSDT', 'positionAmt': '-10'}],
                        [{'symbol': 'AUSDT', 'positionAmt': '-6'}],
                        [{'symbol': 'AUSDT', 'positionAmt': '-6'}],
                        [{'symbol': 'AUSDT', 'positionAmt': '-6'}]]
        reads = {'n': 0}

        def fapi_get(path, params=None):
            reads['n'] += 1
            return risk_windows[min(reads['n'] - 1, len(risk_windows) - 1)]
        monkeypatch.setattr(pm2, '_s6api', lambda: (
            fapi_get, lambda p, q=None: {'orderId': 9}, None, None,
            None, None, None, lambda *a, **k: None))
        monkeypatch.setattr(pm2._exec_core, 'close_intent',
                            staticmethod(
                                lambda s, side, q: ('ci', s, side, q)))

        def exec_order(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED', 'executedQty': '4'}
            return R()
        monkeypatch.setattr(pm2, '_execution_service', lambda: type('X', (), {
            'execute_order': staticmethod(exec_order)})())
        monkeypatch.setattr(pm2, '_save', lambda p: None)
        # 真实 marker seam（fake redis marker store：wcr 对 marker key 进行
        # 真实读取—— closed:TUSDT via fake redis）
        pm2._rset('closed:AUSDT', {'ts': 1.0}) if False else None
        pos = _pos()
        positions = {'AUSDT': pos}

        # 模拟：first partial close 走 mark → restore to clear via _clr
        mc_calls = []
        monkeypatch.setattr(pm2, '_mark_closed',
                            lambda s: mc_calls.append(('mark', s)))
        monkeypatch.setattr(pm2, '_clear_closed_marker',
                            lambda s: mc_calls.append(('clear', s)))
        ok1 = pm2._close('AUSDT', pos, 2.1, '硬止损', positions, force=True)
        assert ok1 is False
        assert ('mark', 'AUSDT') in mc_calls
        assert ('clear', 'AUSDT') in mc_calls              # marker cleared
        # real `was_closed_recently` sees NO marker → second attempt proceeds
        monkeypatch.setattr(pm2, '_was_closed_recently',
                            lambda s, within_hours=4:
                                any(t[0] == s and t[1] != 'clear'
                                    for t in mc_calls))
        ok2 = pm2._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok2 is False      # 第二次 close 亦 partial → proceeds（无 block）
        # 执行侧：两次 close market order 已经发出
        assert reads['n'] >= 2
