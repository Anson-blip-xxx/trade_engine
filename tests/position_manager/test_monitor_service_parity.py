"""P7-05B：PositionMonitoringService 直接构造 parity（exit priority / chain 序）。

legacy expected chain — pm 模块路径（`pm._monitor_one` wrapper，
IO 经 pm 模块 monkeypatch）
vs
service chain — 直接构造 PositionMonitoringService（同一注入集）。

重点：多退出条件同时命中时 exit reason 不变；PMB-18/22 阻断语义不变。
"""
import time as time_mod

import pytest

from shared import position_manager as pm
from position_monitoring import service as mon

CFG = {'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
       'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {},
       'extend_rsi_min': 60, 'extend_funding_min': 0.0005,
       'time_extend_min': 60, 'trail': {'base_mult': 0.3}}


class _DC:
    def __init__(self, k15=[], k1h=[]):
        self.k15, self.k1h = k15, k1h

    def get_klines(self, s, t, c):
        return self.k15 if t == '15m' else self.k1h


def _pos(**over):
    pos = {'entry': 100.0, 'open_time': time_mod.time() - 30 * 60,
           'side': 'SHORT', 'qty': 10.0}
    pos.update(over)
    return dict(pos)


def _run_both(monkeypatch, *, price=100.0, cfg=None, funding=0.0,
              trail=None, k1h=None, elm=False, stag=False,
              close_calls=None, extend_gates=None, pos_over=None):
    """同时运行 legacy 与 direct-service 两条路径；返回 (r_legacy,
    r_service, close_calls_legacy, close_calls_service)。"""
    cfg = cfg or dict(CFG)
    cl = close_calls if close_calls is not None else []
    cs = []

    def lclose(symbol, p, price_, reason, pos):
        cl.append((symbol, price_, reason))
        return True

    monkeypatch.setattr(pm, '_s6api', lambda: (
        None, None, None, lambda s: price, None,
        lambda s: (0, 0, 0.001), lambda s: 70.0, None))
    monkeypatch.setattr(pm, '_get_cfg', lambda p: cfg)
    monkeypatch.setattr(pm, '_get_funding_rate', lambda s: funding)
    monkeypatch.setattr(pm, '_close', lclose)
    monkeypatch.setattr(pm, '_get_data_cache', lambda: _DC(k1h=k1h))
    monkeypatch.setattr(pm, '_early_loss_momentum_weak', lambda k, s: elm)
    monkeypatch.setattr(pm, '_is_stagnant_profit', lambda *a, **k: stag)
    monkeypatch.setattr(pm, '_update_stop_loss', lambda *a, **k: None)
    monkeypatch.setattr(pm, '_peak_pullback_check', lambda *a, **k: None)
    monkeypatch.setattr(pm, '_calc_trail_sl', lambda *a, **k: trail)

    def sclose(symbol, p, price_, reason, pos):
        cs.append((symbol, price_, reason))
        return True

    svc = mon.PositionMonitoringService(
        now=time_mod.time, log=lambda *a, **k: None,
        load=lambda: {}, save=lambda p: None, m1=lambda *a: None,
        gcl=lambda *a, **k: [], gq=[], summ=lambda: None,
        s6=lambda: (None, None, None, lambda s: price, None,
                    lambda s: (0, 0, 0.001), lambda s: 70.0, None),
        ghb=lambda: 0.0, shb=lambda v: None,
        cfg=lambda p: cfg, fund=lambda s, **kw: funding,
        cls=sclose, dc=lambda: _DC(k1h=k1h),
        elm=lambda k, s: elm, stag=lambda *a, **k: stag,
        gate=lambda pnl: pnl >= 0, us=lambda *a, **k: None,
        pc=lambda *a, **k: None, rq=lambda s, q: round(q, 6),
        pp=lambda *a, **k: None, cts=lambda *a, **k: trail,
        pt=lambda *a, **k: None, wst=lambda: False,
        wcr=lambda s: False, trgt=lambda *a, **k: True,
        mc=lambda s: None, lkey='ws:leader', lttl=45, inst='x-1',
        lkfn=lambda: '', wsf=lambda: '' if False else '')

    pos_over = pos_over or {}
    r1 = pm._monitor_one('TUSDT', _pos(**pos_over), {'TUSDT': {}})
    r2 = svc.monitor_one('TUSDT', _pos(**pos_over), {'TUSDT': {}})
    return r1, r2, cl, cs


class TestExitPriorityParity:
    def test_fund_kill_beats_hard_sl(self, monkeypatch):
        r1, r2, cl, cs = _run_both(monkeypatch, funding=-0.006, price=110.0)
        exp = ('资金费率过高 -0.6000%', 110.0, 100.0, 10.0, 'SHORT')
        assert r1 == r2 == exp
        assert [c[2] for c in cl] == [c[2] for c in cs] == [exp[0]]

    def test_hard_sl_beats_urgent(self, monkeypatch):
        r1, r2, cl, cs = _run_both(monkeypatch, price=110.0,
                                   pos_over=dict(sl=105.0))
        exp = ('硬止损', 110.0, 100.0, 10.0, 'SHORT')
        assert r1 == r2 == exp
        assert [c[2] for c in cl] == [c[2] for c in cs] == [exp[0]]

    def test_urgent_beats_early_loss(self, monkeypatch):
        r1, r2, cl, cs = _run_both(
            monkeypatch, price=106.0,
            pos_over=dict(open_time=time_mod.time() - 3000))
        exp = ('紧急止损 pnl=-6.0%', 106.0, 100.0, 10.0, 'SHORT')
        assert r1 == r2 == exp

    def test_stagnant_beats_be_done(self, monkeypatch):
        r1, r2, cl, cs = _run_both(monkeypatch, stag=True)
        assert r1 == r2 and r1[0].startswith('低收益停滞')

    def test_trail_exit_parity(self, monkeypatch):
        r1, r2, cl, cs = _run_both(monkeypatch, price=97.0)
        assert r1 is None and r2 is None        # 需 be_done 才进 trail 分支

    def test_trail_exit_with_be_done(self, monkeypatch):
        cl, cs = [], []

        def lclose(symbol, p, price_, reason, pos):
            cl.append((symbol, price_, reason))
            return True

        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 97.0, None, None, None, None))
        monkeypatch.setattr(pm, '_get_cfg', lambda p: dict(CFG))
        monkeypatch.setattr(pm, '_close', lclose)
        monkeypatch.setattr(pm, '_get_data_cache',
                            lambda: _DC(k1h=None))
        monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                            lambda k, s: False)
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda *a, **k: False)
        monkeypatch.setattr(pm, '_update_stop_loss', lambda *a, **k: None)
        monkeypatch.setattr(pm, '_peak_pullback_check',
                            lambda *a, **k: None)
        monkeypatch.setattr(pm, '_calc_trail_sl', lambda *a, **k: 'exit')

        def sclose(symbol, p, price_, reason, pos):
            cs.append((symbol, price_, reason))
            return True

        svc = mon.PositionMonitoringService(
            now=time_mod.time, log=lambda *a, **k: None,
            load=lambda: {}, save=lambda p: None, m1=lambda *a: None,
            gcl=lambda *a, **k: [], gq=[], summ=lambda: None,
            s6=lambda: (None, None, None, lambda s: 97.0, None,
                        lambda s: (0, 0, 0.001), lambda s: 70.0, None),
            ghb=lambda: 0.0, shb=lambda v: None,
            cfg=lambda p: dict(CFG), fund=lambda s, **kw: 0.0,
            cls=sclose, dc=lambda: _DC(k1h=None),
            elm=lambda k, s: False, stag=lambda *a, **k: False,
            gate=lambda pnl: pnl >= 0, us=lambda *a, **k: None,
            pc=lambda *a, **k: None, rq=lambda s, q: round(q, 6),
            pp=lambda *a, **k: None, cts=lambda *a, **k: 'exit',
            pt=lambda *a, **k: None, wst=lambda: False,
            wcr=lambda s: False, trgt=lambda *a, **k: True,
            mc=lambda s: None, lkey='ws:leader', lttl=45, inst='x-1',
            lkfn=lambda: '', wsf=lambda: '')

        r1 = pm._monitor_one('TUSDT', _pos(be_done=True), {'TUSDT': {}})
        r2 = svc.monitor_one('TUSDT', _pos(be_done=True), {'TUSDT': {}})
        exp = ('趋势反转', 97.0, 100.0, 10.0, 'SHORT')
        assert r1 == r2 == exp
        assert [c[2] for c in cl] == [c[2] for c in cs] == [
            '趋势反转（2次收>EMA20）']
