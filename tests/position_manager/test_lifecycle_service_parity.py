"""P7-07B：PositionLifecycleService 直接构造 parity（open/close/partial）。

legacy expected chain（pm wrapper，pm monkeypatch seam）
vs
direct service chain（直接构造注入）——结果与执行序一致。
"""
import time as time_mod

import pytest

from shared import position_manager as pm
from position_lifecycle import service as lcs


def build_svc(rec=None, load=None, save=None, log=None, sandbox=False,
              price=None, wcr=None, executed=10):
    """Direct-construction helper。返回 (svc, calls)。"""
    calls = {'rec': [], 'pg': [], 'save': [], 'cxa': [], 'logs': [],
             'wkr': [], 'enq': [], 'clr': [], 'mc': []}

    def default_rec(*a, **kw):
        calls['rec'].append((a, kw))
        return True
    rec = rec if rec is not None else default_rec

    def default_pg(ev):
        calls['pg'].append(ev)

    def default_log(m):
        calls['logs'].append(str(m))

    real_s6 = (lambda p, params=None: [],

               lambda p, params=None: {'ok': 1},
               lambda *a, **k: {},
               price or (lambda s: 2.0),

               lambda s: (6, 6), lambda *a, **k: (0, 0, 0),
               lambda *a, **k: 50.0, rec)

    class _Exec:
        def execute_order(self, intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED',
                       'executedQty': str(executed)}
            return R()

    def default_wcr(s):
        return False

    def lmc(s):
        calls['mc'] = calls.get('mc', []) + [s]

    def lc2m(s):
        calls['clr'] = calls.get('clr', []) + [s]

    def save_fn(p):
        calls['save'].append(dict(p))
    svc = lcs.PositionLifecycleService(
        log=log or default_log, now=time_mod.time,
        load=load or (lambda: {}), save=save_fn,
        wcr=wcr if wcr is not None else default_wcr,
        mc=lmc, clr=lc2m,
        s6=lambda: real_s6, sandbox=lambda: sandbox,
        wkr=lambda: calls['wkr'].append(1),
        enq=lambda sym, side, sl, q: calls['enq'].append(
            (sym, side, sl, q)),
        acx=lambda aid: ('acx', aid := aid),
        cxa=lambda s: calls['cxa'].append(s),
        pg=default_pg, rq=lambda s, q: round(q, 6),
        posid=lambda s, p: p.get('position_id') or f'{p.get("system")}:{p.get("entry")}',
        exec_fn=lambda: _Exec(),
        close_fn=lambda *a, **kw: None,          # parity 不经 close_position
        ci=lambda s, side, q: ('ci', s, side, q),
        pi=lambda s, side, q: ('pi', s, side, q),
        lce=lambda s, m, interval=60: None,
    )
    return svc, calls


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0, 'sl': 1.5}
    pos.update(over)
    return pos


class TestOpenParity:
    def test_success_open_both_paths(self, monkeypatch):
        # legacy：pm monkeypatch seam
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        monkeypatch.setattr(pm, '_load', lambda: {})
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)

        def fake_rec(*a, **kw):
            return None

        def fake_s6():
            return (None, lambda p, q=None: {'ok': 1}, None,
                    None, None, None, None, fake_rec)
        monkeypatch.setattr(pm, '_s6api', fake_s6)
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, side, sl, q: None)
        r1 = pm.open_position('NEWUSDT', 'SHORT', 2.0, 10.0, 3, 1.9,
                              system='S6', signal_type='PULSE_DOWN',
                              score=70)
        # direct
        svc, calls = build_svc(executed=10)
        r2 = svc.open_position('NEWUSDT', 'SHORT', 2.0, 10.0, 3, 1.9,
                               system='S6', signal_type='PULSE_DOWN',
                               score=70)
        assert r1 == r2 is True

    def test_close_full_both_paths_match(self, monkeypatch):
        # legacy：pm _close 全 spy
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm, '_mark_closed', lambda s: None)
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        monkeypatch.setattr(pm, '_pg_record_event',
                            lambda ev: None)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], lambda p, q=None: {'orderId': 9},
            None, None, None, None, None,
            lambda *a, **k: None))
        monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))

        class FakeSvc:
            def __init__(self):
                self.rawO = {'orderId': 9, 'status': 'FILLED',
                             'executedQty': '10'}
        monkeypatch.setattr(pm._exec_core, 'close_intent',
                            staticmethod(
                                lambda s, side, q: ('ci', s, side, q)))

        def executeOrder(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED',
                       'executedQty': '10'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    executeOrder)})())
        pos = _pos()
        positions = {'AUSDT': pos}
        state = {}

        def save(p):
            state.update(p)
        monkeypatch.setattr(pm, '_save', save)
        monkeypatch.setattr(pm, '_load', lambda: dict(state))
        r1 = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        positions.pop('AUSDT', None)
        pos2 = _pos()
        positions_2 = {'AUSDT': pos2}
        svc, calls = build_svc(executed=10)
        r2 = svc.close('AUSDT', pos2, 2.1, '硬止损', positions_2)
        assert r1 == r2 is True
        assert 'AUSDT' not in positions and 'AUSDT' not in positions_2


class TestPartialParity:
    def test_partial_qty_mutation(self, monkeypatch):
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, None))
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(
                                lambda s, side, q: ('pi', s, side, q)))

        def executeOrder(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    executeOrder)})())
        monkeypatch.setattr(pm, '_save', lambda p: None)
        pos = _pos()
        pos['qty'] = 10.0
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, positions)
        assert pos['qty'] == 6.0

        pos2 = _pos()
        pos2['qty'] = 10.0
        svc, calls = build_svc(executed=4)
        positions2 = {'AUSDT': pos2}
        svc.partial_close('AUSDT', pos2, 2.1, 4.0, 2.0, positions2)
        assert pos2['qty'] == 6.0     # 同一语义

    def test_negative_partial_increases_qty(self, monkeypatch):
        """E-OBS-7 冻结：负数 close_qty 让 qty **增大**。"""
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, None))
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(
                                lambda s, side, q: ('pi', s, side, q)))

        def executeOrder(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    executeOrder)})())
        monkeypatch.setattr(pm, '_save', lambda p: None)
        pos = _pos()
        pos['qty'] = 10.0
        pm._partial_close('AUSDT', pos, 2.1, -2.0, 2.0,
                          {'AUSDT': pos})
        assert pos['qty'] == 12.0
