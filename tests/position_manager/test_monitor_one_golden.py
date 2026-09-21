"""P7-05A：_monitor_one 出场链 golden（11 步顺序 + exit reason priority）。

冻结步序（每步命中且 _close 成功 → 返回 (reason, price, entry, qty, side)）：
  0 资金费率 kill（SHORT<-0.5% / LONG>+0.5%）→ 警告级 ±0.2% 一次性（fund_warned）
  1 sl_breached → 硬止损
  2 pnl < cfg.sl_breach_max → 紧急止损
  3 hold>=5 且 pnl<=-2 → 早期亏损保护（15m 动量确认；异常吞错续链）
  4 低收益停滞（_is_stagnant_profit）
  3.5 be_done 升级（pnl>=be_pct → _update_stop_loss 一次）
  4.5 分层止盈（阈值升序、每次只触发一层、tp_done 记账、安全拆仓）
  5 追踪锁利（be_done 后；'exit' → close 返回 reason='趋势反转'；数值 → _place_trail_sl）
  5.5 峰值回撤（str → close；float → _place_trail_sl）
  6 1h EMA 安全阀（hold<60 或 pnl>=40 豁免；V2修正：亏损观察仍进入时间止损）
  7 时间止损（hold>ts_min；浮亏可延期一次；微盈直接平）
"""
import time

import pytest

from shared import position_manager as pm

CFG = {'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
       'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {},
       'extend_rsi_min': 60, 'extend_funding_min': 0.0005,
       'time_extend_min': 60, 'trail': {'base_mult': 0.3}}


class Knob:
    def __init__(self):
        self.df = 0.5
        self.trail = None


@pytest.fixture
def one(monkeypatch):
    n = Knob()
    n.price = 100.0
    n.funding = 0.0
    n.rsi = 0.0
    n.oi_funding = (0, 0, 0.0)
    n.klines15 = []
    n.klines1h = []
    n.close_calls = []
    n.close_result = True
    n.stop_calls = []
    n.place_calls = []
    n.partial_calls = []
    n.logs = []
    n.momentum_weak = True

    monkeypatch.setattr(pm, '_s6api', lambda: (
        None, None, None, lambda s: n.price, None, None, None, None))
    monkeypatch.setattr(pm, '_get_funding_rate', lambda s: n.funding)
    monkeypatch.setattr(pm, '_get_cfg', lambda pos: dict(CFG))

    class _DC:
        def get_klines(self, sym, tf, cnt):
            return n.klines15 if tf == '15m' else n.klines1h
    monkeypatch.setattr(pm, '_get_data_cache', lambda: _DC())
    monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                        lambda k, side: n.momentum_weak)
    def _close(s, p, price, reason, pos, **kw):
        n.close_calls.append((s, price, reason))
        return n.close_result
    monkeypatch.setattr(pm, '_close', _close)
    monkeypatch.setattr(pm, '_update_stop_loss',
                        lambda s, p, price, entry:
                        n.stop_calls.append((s, price)))
    monkeypatch.setattr(pm, '_calc_trail_sl',
                        lambda s, p, price, cfg, pos: n.trail)
    monkeypatch.setattr(pm, '_place_trail_sl',
                        lambda s, p, sl, pos:
                        n.place_calls.append((s, sl)))
    monkeypatch.setattr(pm, '_partial_close',
                        lambda s, p, price, q, tp, pos:
                        n.partial_calls.append((s, q, tp)))
    monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))

    def base(**over):
        pos = {'entry': 100.0, 'open_time': time.time() - 30 * 60,
               'side': 'SHORT', 'qty': 10.0}
        pos.update(over)
        return dict(pos)
    n.base = base
    n.mon = lambda pos: pm._monitor_one('TUSDT', pos, {'TUSDT': pos})
    return n


class TestFundRate:
    def test_short_fund_kill(self, one):
        one.funding = -0.006
        r = one.mon(one.base())
        assert one.close_calls[0][2].startswith('资金费率过高')
        assert r[0] == '资金费率过高 -0.6000%' and r[2] == 100.0 and r[4] == 'SHORT'

    def test_close_fail_returns_none(self, one):
        one.funding = -0.006
        one.close_result = False
        assert one.mon(one.base()) is None

    def test_long_fund_kill(self, one):
        one.funding = 0.006
        r = one.mon(one.base(side='LONG'))
        assert r[0].startswith('资金费率过高') and r[4] == 'LONG'

    def test_warn_below_kill_one_shot(self, one):
        one.funding = -0.003
        pos = one.base()
        r = one.mon(pos)
        assert r is None and pos['fund_warned'] is True
        logs1 = list(one.logs)
        one.mon(pos)                       # 已 warning → 不重复
        assert pos.get('fund_warned') is True

    def test_warn_under_0_2pct_no_flag(self, one):
        one.funding = -0.001
        pos = one.base()
        one.mon(pos)
        assert 'fund_warned' not in pos


class TestHardSl:
    def test_sl_breached_short(self, one):
        one.price = 110.0
        r = one.mon(one.base(sl=105.0))
        assert r[0] == '硬止损' and one.close_calls[0][2] == '硬止损'

    def test_sl_breached_long(self, one):
        one.price = 90.0
        r = one.mon(one.base(side='LONG', sl=95.0))
        assert r[0] == '硬止损'

    def test_sl_equals_entry_not_breached(self, one):
        one.price = 102.0                  # pnl=-2：sl==entry → 不判死；链上无档位
        one.momentum_weak = False
        assert one.mon(one.base(sl=100.0)) is None

    def test_hard_sl_priority_over_urgent(self, one):
        one.price = 120.0                  # pnl=-20 同时触发两档 → 硬止损优先
        r = one.mon(one.base(sl=105.0))
        assert r[0] == '硬止损' and len(one.close_calls) == 1


class TestUrgentStop:
    def test_below_max_loss(self, one):
        one.price = 106.0                  # SHORT: 涨到 106 → pnl=-6 < -5
        r = one.mon(one.base())
        assert r[0] == '紧急止损 pnl=-6.0%'

    def test_cfg_max_loss_wins(self, one, monkeypatch):
        monkeypatch.setattr(pm, '_get_cfg',
                            lambda pos: dict(CFG, sl_breach_max=-10.0))
        one.price = 106.0                  # pnl=-6 > -10 → 不触发；后续步无档
        one.momentum_weak = False
        assert one.mon(one.base()) is None

    def test_me_not_triggered_at_equal(self, one):
        one.price = 105.0                  # SHORT pnl=-5.0 == max → 不平（严格 <）
        one.momentum_weak = False
        assert one.mon(one.base()) is None


class TestEarlyLoss:
    def test_exits_with_momentum_weak(self, one):
        one.price = 103.0                  # SHORT 涨 3% → pnl=-3
        r = one.mon(one.base(open_time=time.time() - 10 * 60))
        assert r is not None and r[0].startswith('早期亏损保护 pnl=-3.0%')

    def test_hold_under_5_skips(self, one):
        one.price = 103.0
        r = one.mon(one.base(open_time=time.time() - 2 * 60))
        assert r is None

    def test_momentum_strong_continues(self, one, monkeypatch):
        monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                            lambda k, s: False)
        one.price = 103.0
        r = one.mon(one.base(open_time=time.time() - 10 * 60))
        assert r is None

    def test_kline_exception_logged_then_continue(self, one, monkeypatch):
        class _DGK:
            def get_klines(self, s, t, c):
                raise RuntimeError('k')
        monkeypatch.setattr(pm, '_get_data_cache', lambda: _DGK())
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        one.price = 103.0
        r = one.mon(one.base(open_time=time.time() - 10 * 60))
        assert r is None and any('早期亏损保护异常' in l for l in logs)

    def test_early_loss_before_stagnant(self, one, monkeypatch):
        """早期亏损(pnl<=-2) 在停滞判断之前（亏损仓不走停滞档）。"""
        fired = []
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda pnl, hold, *a: fired.append(True) or True)
        one.price = 103.0                  # SHORT pnl=-3 → 早期亏损先命中
        r = one.mon(one.base(open_time=time.time() - 10 * 60))
        assert r[0].startswith('早期亏损保护') and fired == []


class TestStagnant:
    def test_stagnant_profit_exit(self, one, monkeypatch):
        one.funding = 0.0
        pos = one.base()
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda pnl, hold, *a: True)
        r = one.mon(pos)
        assert r is not None and r[0].startswith('低收益停滞 pnl=')


class TestBeDone:
    def test_upgrade_once(self, one):
        one.price = 97.0                   # SHORT 跌到 97 → pnl=+3 >= 2
        pos = one.base()
        one.mon(pos)
        assert len(one.stop_calls) == 1 and one.stop_calls[0] == ('TUSDT', 97.0)
        pos['be_done'] = True
        one.mon(pos)                       # 升级 → 不再
        assert len(one.stop_calls) == 1

    def test_pnl_below_threshold_no_upgrade(self, one):
        one.price = 98.5                   # SHORT pnl=+1.5 < 2
        pos = one.base()
        one.mon(pos)
        assert one.stop_calls == []


class TestPartialTp:
    def test_ascending_one_layer_per_call(self, one):
        import shared.position_manager as pm2
        cfg = dict(CFG, partial_tp={2.0: 0.3, 1.0: 0.5})
        pm2._get_cfg = lambda p: cfg
        one.price = 97.0                   # pnl=+3 命中全部阈值
        pos = one.base()
        one.mon(pos)
        # 升序扫描 → 最低层 1.0 先触发即 break（一层/次）
        assert pos['tp_done'] == [1.0]
        assert one.partial_calls == [('TUSDT', 5.0, 1.0)]

    def test_second_call_higher_tier(self, one):
        cfg = dict(CFG, partial_tp={1.0: 0.5, 2.0: 0.3})
        import shared.position_manager as pm2
        pm2._get_cfg = lambda p: cfg
        one.price = 97.0
        pos = one.base(tp_done=[1.0])
        one.mon(pos)
        assert one.partial_calls == [('TUSDT', 3.0, 2.0)]  # qty*0.3=3

    def test_unsafe_split_skipped(self, one):
        cfg = dict(CFG, partial_tp={1.0: 1.2})   # close_qty=12 > qty=10 → 不拆
        import shared.position_manager as pm2
        pm2._get_cfg = lambda p: cfg
        one.price = 98.5                   # SHORT pnl=1.5 命中 1.0 层
        pos = one.base()
        r = one.mon(pos)
        assert one.partial_calls == [] and pos.get('tp_done') != [1.0]

    def test_partial_only_when_pnl_positive(self, one):
        cfg = dict(CFG, partial_tp={1.0: 0.5})
        import shared.position_manager as pm2
        pm2._get_cfg = lambda p: cfg
        one.price = 101.0                  # SHORT pnl=-1
        one.mon(one.base())
        assert one.partial_calls == []

    def test_pnl_between_tiers_no_fire(self, one):
        cfg = dict(CFG, partial_tp={2.0: 0.5})
        import shared.position_manager as pm2
        pm2._get_cfg = lambda p: cfg
        one.price = 98.5
        one.mon(one.base())
        assert one.partial_calls == []


class TestTrail:
    def test_exit_close_returns_reversal(self, one):
        one.trail = 'exit'
        pos = one.base(be_done=True)
        one.price = 97.0
        r = one.mon(pos)
        assert r[0] == '趋势反转'
        assert one.close_calls[0][2] == '趋势反转（2次收>EMA20）'

    def test_numeric_places_sl(self, one):
        one.trail = 95.0
        pos = one.base(be_done=True)
        one.price = 97.0                   # SHORT pnl=+3
        r = one.mon(pos)
        assert r is None and one.place_calls == [('TUSDT', 95.0)]

    def test_none_no_action(self, one):
        one.trail = None
        pos = one.base(be_done=True)
        r = one.mon(pos)
        assert r is None and one.place_calls == []

    def test_requires_be_done(self, one):
        one.trail = 'exit'
        pos = one.base()                   # 未 be_done
        r = one.mon(pos)
        assert r is None and one.close_calls == []

    def test_requires_pnl_above_threshold(self, one):
        one.trail = 'exit'
        pos = one.base(be_done=True)
        one.price = 99.5                   # SHORT pnl=+0.5 < 2
        r = one.mon(pos)
        assert r is None


class TestPeakGuard:
    def _cfg(self, cfg_val):
        import shared.position_manager as pm2
        pm2._get_cfg = lambda p: dict(CFG, peak_guard=cfg_val)

    def test_str_result_close(self, one):
        cfg = {'trigger_pct': 1.0, 'drawdown_pct': 2.0}
        self._cfg(cfg)
        one.price = 96.0                   # pnl=4 → 武装；armed → ... 用 pullback
        pos = one.base(peak_guard_armed=True, lowest=96.0)
        r = one.mon(pos)                   # pullback 0 < 2 → locked_sl
        assert r is None and one.place_calls[0][1] == round(96.0 * 1.02, 6)

    def test_pullback_exceed_closes(self, one):
        cfg = {'trigger_pct': 1.0, 'drawdown_pct': 2.0}
        self._cfg(cfg)
        one.price = 98.11                  # extreme 96 → pullback=(98.11-96)/96=2.2%
        pos = one.base(peak_guard_armed=True, lowest=96.0)
        r = one.mon(pos)
        assert r is not None and r[0].startswith('峰值回撤保护')
        # reason 逐字包含 pnl 与回撤
        assert 'pnl=1.9%' in r[0] and '回撤2.2%' in r[0]

    def test_no_guard_no_arm(self, one):
        self._cfg({})
        one.price = 90.0                   # SHORT pnl=+10 无 guard
        pos = one.base()
        r = one.mon(pos)
        assert r is None and 'peak_guard_armed' not in pos


class TestOneHReversal:
    def _setup(self, one, hold_min=120, side='SHORT', pnl=0.0):
        one.price = 100.0 + (pnl / 100) * 100
        pos = one.base(side=side, open_time=time.time() - hold_min * 60)
        return pos

    def _set_1h(self, one, side):
        """构造 k1h（21 根）触发对应方向的反转：SHORT ema9>ema20*1.02 /
        LONG ema9<ema20*0.98（闭合上 21 根，均值计算 ema9=后9根均值、
        ema20=后20根均值）。"""
        if side == 'SHORT':
            closes = [50.0] * 12 + [110.0] * 9   # ema9=110 ema20=77
        else:
            closes = [150.0] * 12 + [50.0] * 9   # ema9=50 ema20=105
        one.klines1h = [[0, c, c, c, c] for c in closes]

    def test_fresh_position_exempt(self, one):
        pos = self._setup(one, hold_min=30)
        r = one.mon(pos)
        assert r is None

    def test_big_profit_exempt(self, one):
        one.price = 60.0                   # SHORT pnl=+50 >= 40 豁免
        pos = self._setup(one, hold_min=120)
        r = one.mon(pos)
        assert r is None

    def test_short_reversal_breakeven_closes(self, one, monkeypatch):
        one._setup = self._setup
        self._set_1h(one, 'SHORT')
        monkeypatch.setattr(pm, '_should_exit_1h_reversal', lambda pnl: True)
        pos = self._setup(one, hold_min=120, side='SHORT', pnl=0.0)
        r = one.mon(pos)
        assert r is not None and r[0] == '1h趋势反转'
        assert one.close_calls[0][2] == '1h趋势反转'

    def test_long_reversal_loss_warn_once(self, one, monkeypatch):
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        monkeypatch.setattr(pm, '_should_exit_1h_reversal', lambda pnl: False)
        self._set_1h(one, 'LONG')
        one.momentum_weak = False          # 避开早期亏损档
        pos = self._setup(one, hold_min=120, side='LONG', pnl=-3.0)
        r = one.mon(pos)
        assert r is None                    # 不平仓，交止损/时间止损
        assert any('1h反转观察' in l for l in logs)
        assert pos.get('trend_reversal_warned') is True
        r = one.mon(pos)                    # 第二轮不再警告
        assert sum('1h反转观察' in l for l in logs) == 1

    def test_hold_under_60_no_klines_fetch(self, one, monkeypatch):
        monkeypatch.setattr(pm, '_should_exit_1h_reversal', lambda pnl: True)
        pos = self._setup(one, hold_min=30)   # hold<60 → pass 分支
        r = one.mon(pos)
        assert r is None


class TestTimeStop:
    @pytest.mark.parametrize('side', ['SHORT', 'LONG'])
    @pytest.mark.parametrize('warned', [False, True])
    def test_losing_reversal_reaches_expired_time_stop(self, one, side, warned):
        TestOneHReversal()._set_1h(one, side)
        one.price = 103.0 if side == 'SHORT' else 97.0
        one.momentum_weak = False
        pos = one.base(side=side, open_time=time.time() - 250 * 60,
                       trend_reversal_warned=warned)
        assert one.mon(pos)[0] == '时间止损'
        assert len(one.close_calls) == 1
        assert pos['trend_reversal_warned'] is True

    @pytest.mark.parametrize('side', ['SHORT', 'LONG'])
    @pytest.mark.parametrize('raises', [False, True])
    def test_failed_reversal_close_does_not_attempt_time_stop(
            self, one, monkeypatch, side, raises):
        TestOneHReversal()._set_1h(one, side)
        one.price = 100.0
        one.close_result = False
        if raises:
            def uncertain_close(symbol, pos, price, reason, positions):
                one.close_calls.append((symbol, price, reason))
                raise TimeoutError('response unavailable')
            monkeypatch.setattr(pm, '_close', uncertain_close)
        pos = one.base(side=side, open_time=time.time() - 250 * 60)
        assert one.mon(pos) is None
        assert one.close_calls == [('TUSDT', 100.0, '1h趋势反转')]

    @pytest.mark.parametrize('side', ['SHORT', 'LONG'])
    @pytest.mark.parametrize('remaining', [100, 0, -100])
    def test_reloaded_extension_deadline_without_regrant(
            self, one, monkeypatch, side, remaining):
        now = 1800000000.0
        monkeypatch.setattr(pm.time, 'time', lambda: now)
        one.price = 103.0 if side == 'SHORT' else 97.0
        one.momentum_weak = False
        TestOneHReversal()._set_1h(one, side)
        # Qualifying extension inputs must not grant another extension.
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: one.price, None,
            lambda s: (0, 0, 0.001), lambda s: 70, None))
        pos = one.base(side=side, open_time=now - 250 * 60,
                       time_extended=True, extend_deadline=now + remaining)
        for _ in range(2):
            pos = dict(pos)  # State reloaded between monitor cycles.
            one.close_calls.clear()
            one.close_result = False
            assert one.mon(pos) is None
            assert pos['extend_deadline'] == now + remaining
            assert len(one.close_calls) == (0 if remaining > 0 else 1)
            if one.close_calls:
                assert one.close_calls[0][2] == '时间止损'

    @pytest.mark.parametrize('side', ['SHORT', 'LONG'])
    def test_extension_does_not_override_hard_stop(self, one, side):
        one.price = 103.0 if side == 'SHORT' else 97.0
        pos = one.base(side=side, open_time=time.time() - 250 * 60,
                       sl=102.0 if side == 'SHORT' else 98.0,
                       time_extended=True, extend_deadline=time.time() + 100)
        assert one.mon(pos)[0] == '硬止损'
        assert len(one.close_calls) == 1

    def test_micro_profit_closes(self, one):
        # hold=250min, pnl=+1（<be 2）
        one.price = 99.0                   # SHORT pnl=+1（<be 2）
        pos = one.base(open_time=time.time() - 250 * 60)
        r = one.mon(pos)
        assert r[0] == '时间止损'

    def test_loss_extend_once(self, one):
        one.rsi = 70
        one.oi_funding = (0, 0, 0.001)
        n = one
        import shared.position_manager as pm2

        def s6():
            return (None, None, None, lambda s: n.price, None,
                    lambda s: n.oi_funding, lambda s: n.rsi, None)
        pm2._s6api = s6                    # 时间延期走 RSI/funding API
        cfg = dict(CFG, extend_rsi_min=60, extend_funding_min=0.0005,
                   time_extend_min=60, time_stop_min=240)
        pm2._get_cfg = lambda p: cfg
        one.price = 103.0                  # SHORT pnl=-3
        one.momentum_weak = False
        before = time.time()
        pos = one.base(open_time=time.time() - 250 * 60)
        r = one.mon(pos)
        assert r is None and pos.get('time_extended') is True
        assert pos.get('extend_deadline', 0) - before > 3599

    def test_extend_needs_both_gates(self, one):
        n = one
        import shared.position_manager as pm2

        def s6():
            return (None, None, None, lambda s: n.price, None,
                    lambda s: n.oi_funding, lambda s: n.rsi, None)
        pm2._s6api = s6
        cfg = dict(CFG, extend_rsi_min=60, extend_funding_min=0.0005)
        pm2._get_cfg = lambda p: cfg
        one.rsi = 70
        one.oi_funding = (0, 0, 0.0)      # funding 未达 0.0005
        one.price = 103.0
        one.momentum_weak = False
        pos = one.base(open_time=time.time() - 250 * 60)
        r = one.mon(pos)
        assert r[0] == '时间止损' and 'time_extended' not in pos

    def test_time_extended_flag_honors_deadline(self, one, monkeypatch):
        """V2 intentional correction: persisted extension remains effective."""
        cfg = dict(CFG, time_stop_min=240)
        monkeypatch.setattr(pm, '_get_cfg', lambda p: cfg)
        pos = one.base(open_time=time.time() - 250 * 60,
                       time_extended=True,
                       extend_deadline=time.time() + 100)
        one.price = 103.0                  # 浮亏分支
        one.momentum_weak = False
        r = one.mon(pos)
        assert r is None and one.close_calls == []

    def test_preexisting_deadline_still_open_skips(self, one, monkeypatch):
        """未延期但残留 extend_deadline 未到 → 回到延期观察（return None）。"""
        cfg = dict(CFG, time_stop_min=240)
        monkeypatch.setattr(pm, '_get_cfg', lambda p: cfg)
        pos = one.base(open_time=time.time() - 250 * 60,
                       extend_deadline=time.time() + 100)
        one.price = 103.0
        one.momentum_weak = False
        r = one.mon(pos)
        assert r is None

    def test_extended_deadline_expired_closes(self, one, monkeypatch):
        """未延期 + deadline 已过 → 平仓（不再延长）。"""
        cfg = dict(CFG, time_stop_min=240)
        monkeypatch.setattr(pm, '_get_cfg', lambda p: cfg)
        pos = one.base(open_time=time.time() - 250 * 60,
                       extend_deadline=time.time() - 100)
        one.price = 103.0
        one.momentum_weak = False
        r = one.mon(pos)
        assert r[0] == '时间止损'
    def test_profit_covers_be_done_no_time_stop(self, one):
        one.price = 96.5                   # SHORT pnl=+3.5 >= be 2 → 时间到也不平
        pos = one.base(open_time=time.time() - 300 * 60)
        r = one.mon(pos)
        assert r is None
