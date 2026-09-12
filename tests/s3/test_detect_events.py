"""P5-01：detect_events 全 event family golden（含阈值三点/强度/守卫/S3-3）。

全部调真实 detect_events（windows 直接构造，不走 fetch）；
expected 只写死（不复制算法）。
"""
import pytest

from conftest import w15m, w1h, w24h, windows, detect

SYM = 'TESTUSDT'


def types(events):
    return [e['type'] for e in events]


# ── 无事件基线 ──────────────────────────────────────────────────────────

def test_no_events_baseline(s3m):
    assert detect(s3m, windows(w15m())) == []


# ── PULSE_UP / PULSE_DOWN ──────────────────────────────────────────────

class TestPulse:
    def test_pulse_up_trigger_and_strength(self, s3m):
        evs = detect(s3m, windows(w15m(chg=6.0, vol_ratio=2.0)))
        assert evs[0]['type'] == 'PULSE_UP'          # 源码序：PULSE 先于 BOOST 类
        e = evs[0]
        assert e['symbol'] == SYM
        assert e['strength'] == 48                      # int(6*8+0*4)=48, floor 20
        assert e['chg_15m'] == 6.0
        assert e['chg_1h'] == 0.0
        assert 'side' not in e                          # 无 side 字段（S3 事实）
        assert 'ts' not in e                            # 每事件无独立 ts
        assert 'breakout_confirmed' not in e            # 无 breakout 附加（S3-3 路径）

    @pytest.mark.parametrize('chg15,expect', [(4.99, False), (5.0, True),
                                              (5.01, True)])
    def test_pulse_up_15m_threshold_boundary(self, s3m, chg15, expect):
        w = windows(w15m(chg=chg15, vol_ratio=2.0))
        evs = detect(s3m, w)
        assert ('PULSE_UP' in types(evs)) is expect

    @pytest.mark.parametrize('vol,expect', [(1.49, False), (1.5, True),
                                            (2.0, True)])
    def test_pulse_up_vol_ratio_boundary(self, s3m, vol, expect):
        w = windows(w15m(chg=6.0, vol_ratio=vol))
        assert ('PULSE_UP' in types(detect(s3m, w))) is expect

    def test_pulse_up_1h_branch(self, s3m):
        """1h chg>=8 也触发（vol 15m>=1.5 仍然必需）。"""
        w = windows(w15m(chg=0.0, vol_ratio=2.0), w1h(chg=8.0))
        # w1h.chg=8 但 4h chg 0 → 无 TREND（需要 4h>=2）
        assert 'PULSE_UP' in types(detect(s3m, w))

    def test_pulse_down_trigger_strength_neg(self, s3m):
        evs = detect(s3m, windows(w15m(chg=-6.0, vol_ratio=2.0)))
        assert evs[0]['type'] == 'PULSE_DOWN'
        assert evs[0]['strength'] == 48                 # abs 计算同式
        assert 'side' not in evs[0]

    @pytest.mark.parametrize('chg15,expect', [(-3.99, False), (-5.0, True),
                                              (-5.01, True)])
    def test_pulse_down_also_fires_at_minus5(self, s3m, chg15, expect):
        """PULSE_DOWN 与 PULSE_UP 非互斥：-5 同时仅 PULSE_DOWN（阈值 -5.0 独立）。"""
        w = windows(w15m(chg=chg15, vol_ratio=2.0))
        evs = detect(s3m, w)
        assert ('PULSE_DOWN' in types(evs)) is expect

    def test_both_pulse_types_mutually_exclusive_strength(self, s3m):
        """chg 不会同时 ≥5 与 ≤-5 → 两大类互斥出现。"""
        evs = detect(s3m, windows(w15m(chg=10.0, vol_ratio=3.0)))
        assert 'PULSE_DOWN' not in types(evs)
        evs2 = detect(s3m, windows(w15m(chg=-10.0, vol_ratio=3.0)))
        assert 'PULSE_UP' not in types(evs2)


# ── PULSE 超买/超卖 guard ──────────────────────────────────────────────

class TestPulseGuards:
    def test_pulse_up_skipped_when_overbought(self, s3m):
        """价格偏离 4h EMA20 > 3×ATR% → PULSE_UP 跳过（log 分支，仍无事件）。"""
        w = windows(w15m(close=120.0, chg=6.0, vol_ratio=2.0),
                    h4={'chg': 0.0, 'ema20': 100.0, 'atr_pct': 2.0,
                         'high': 120.0, 'low': 90.0, 'volatility': 0.0,
                         'close': 120.0})
        assert 'PULSE_UP' not in types(detect(s3m, w))  # 无 breakout 解封路径

    def test_pulse_up_guard_released_by_breakout_false_because_close_pos_absent(
            self, s3m):
        """S3-3：真实窗口无 close_pos → _strong_breakout 恒 False → guard 不可解除。"""
        w = windows(w15m(close=120.0, chg=6.0, vol_ratio=2.0),
                    h4={'chg': 0.0, 'ema20': 100.0, 'atr_pct': 2.0,
                         'high': 120.0, 'low': 90.0, 'volatility': 0.0,
                         'close': 120.0})
        assert 'PULSE_UP' not in types(detect(s3m, w))  # breakout_confirmed 不出现

    def test_close_pos_injected_makes_breakout_confirmed_true(self, s3m):
        """UNIT 级事实：仅当调用方注入 close_pos/taker/vol 快照时 breakout 才可达；
        真实 producer 不写 close_pos（S3-3 冻结）。"""
        w15 = w15m(close=120.0, chg=6.0, vol_ratio=2.0,
                   taker_ratio=0.6)
        w15['close_pos'] = 70.0                          # 人为补 producers 不写字段
        w = windows(w15,
                    h4={'chg': 1.0, 'ema20': 100.0, 'atr_pct': 2.0,
                        'high': 120.0, 'low': 90.0, 'volatility': 0.0,
                        'close': 120.0},
                    h1={'chg': 1.0, 'volatility': 0.0, 'high': 120.0,
                        'low': 90.0, 'ema20': 100.0, 'ema60': 90.0,
                        'atr_pct': 0.1, 'close': 120.0})
        assert 'PULSE_UP' in types(detect(s3m, w))
        assert detect(s3m, w)[0].get('breakout_confirmed') is True


# ── PANIC_SELL ────────────────────────────────────────────────────────

class TestPanicSell:
    @pytest.mark.parametrize('chg,expect', [(-3.99, False), (-4.0, True),
                                            (-4.01, True)])
    def test_panic_boundary(self, s3m, chg, expect):
        w = windows(w15m(chg=chg, vol_ratio=2.5))
        assert ('PANIC_SELL' in types(detect(s3m, w))) is expect

    @pytest.mark.parametrize('vol,expect', [(1.99, False), (2.0, True)])
    def test_panic_vol_boundary(self, s3m, vol, expect):
        w = windows(w15m(chg=-5.0, vol_ratio=vol))
        assert ('PANIC_SELL' in types(detect(s3m, w))) is expect

    def test_panic_strength_floor_30(self, s3m):
        evs = detect(s3m, windows(w15m(chg=-4.0, vol_ratio=2.0)))
        panic = [e for e in evs if e['type'] == 'PANIC_SELL'][0]
        assert panic['strength'] == 48                   # |−4|*12=48 ≥ floor 30

    def test_panic_overbought_guard_oversold_skip(self, s3m):
        """价格跌穿下轨 → PANIC_SELL 跳过（_is_oversold）。"""
        w = windows(w15m(close=80.0, chg=-5.0, vol_ratio=2.0,
                         high=100.0, low=80.0),
                    h4={'chg': 0.0, 'ema20': 100.0, 'atr_pct': 2.0,
                         'high': 100.0, 'low': 80.0, 'volatility': 0.0,
                         'close': 80.0})
        evs = detect(s3m, w)
        # PULSE_DOWN/PANIC guard 均触发 → 只剩中性 HIGH_VOL
        assert 'PULSE_DOWN' not in types(evs) and 'PANIC_SELL' not in types(evs)


# ── VIOLENT ───────────────────────────────────────────────────────────

class TestViolent:
    @pytest.mark.parametrize('vol1,vol4,expect', [
        (14.9, 0.0, False), (15.0, 0.0, True), (0.0, 23.9, False),
        (0.0, 24.0, True)])
    def test_violent_threshold(self, s3m, vol1, vol4, expect):
        w = windows(w15m(),
                    w1h(chg=0.0, volatility=vol1, high=105.0, low=95.0),
                    h4={'chg': 0.0, 'volatility': vol4, 'ema20': 100.0,
                         'ema60': 100.0, 'atr_pct': 0.05, 'high': 105.0,
                         'low': 95.0, 'close': 100.0})
        evs = [e for e in detect(s3m, w) if e['type'].startswith('VIOLENT')]
        assert (len(evs) == 1) is expect

    def test_violent_direction_by_mid(self, s3m):
        w = windows(w15m(close=104.0),                    # 上半段 → BULLISH
                    w1h(chg=0, volatility=20.0, high=110.0, low=90.0),
                    h4={'chg': 0.0, 'volatility': 20.0, 'ema20': 100.0,
                         'ema60': 100.0, 'atr_pct': 0.05, 'high': 110.0,
                         'low': 90.0, 'close': 104.0})
        evs = detect(s3m, w)
        assert types(evs) == ['VIOLENT_BULLISH']
        assert evs[0]['strength'] == 60                   # min(99, int(20*3))=60,≥30
        assert evs[0]['vol_1h'] == 20.0
        assert 'side' not in evs[0]                       # 方向仅在类型名

    def test_violent_bearish(self, s3m):
        w = windows(w15m(close=96.0),                     # 下半段
                    w1h(chg=0, volatility=20.0, high=110.0, low=90.0),
                    h4={'chg': 0.0, 'volatility': 20.0, 'ema20': 100.0,
                         'ema60': 100.0, 'atr_pct': 0.05, 'high': 110.0,
                         'low': 90.0, 'close': 96.0})
        assert types(detect(s3m, w)) == ['VIOLENT_BEARISH']


# ── PUMP_UP / PUMP_DOWN ──────────────────────────────────────────────

class TestPump:
    @pytest.mark.parametrize('chg15,expect', [(7.99, False), (8.0, True),
                                              (8.01, True)])
    def test_pump_up_boundary(self, s3m, chg15, expect):
        w = windows(w15m(chg=chg15, vol_ratio=2.5))
        assert ('PUMP_UP' in types(detect(s3m, w))) is expect

    def test_pump_strength_floor_30_and_fields(self, s3m):
        evs = detect(s3m, windows(w15m(chg=8.0, vol_ratio=2.5)))
        pump = [e for e in evs if e['type'] == 'PUMP_UP'][0]
        assert pump['strength'] == 64                     # int(8*8)=64 ≥30
        assert pump['vol_ratio'] == 2.5
        assert 'side' not in pump

    def test_pump_down_floor(self, s3m):
        evs = detect(s3m, windows(w15m(chg=-8.5, vol_ratio=2.2)))
        pump = [e for e in evs if e['type'] == 'PUMP_DOWN'][0]
        assert pump['strength'] == 68                     # abs(-8.5)*8=68

    def test_pulse_and_pump_coexist(self, s3m):
        """PULSE(5~8) 与 PUMP(>=8) 阈值重叠区 → 两个事件共存（真实行为）。"""
        evs = detect(s3m, windows(w15m(chg=8.0, vol_ratio=2.5)))
        assert set(types(evs)) == {'PULSE_UP', 'PUMP_UP', 'HIGH_VOL'}


# ── TREND ─────────────────────────────────────────────────────────────

class TestTrend:
    @pytest.mark.parametrize('chg1h,expect', [(0.99, False), (1.0, True)])
    def test_trend_up_1h_boundary(self, s3m, chg1h, expect):
        w = windows(w15m(), w1h(chg=chg1h),
                    h4={'chg': 2.0, 'ema20': 100.0, 'ema60': 100.0,
                         'atr_pct': 0.05, 'high': 101.0, 'low': 99.0,
                         'volatility': 0.0, 'close': 100.0})
        assert types(detect(s3m, w)).count('TREND_UP') == (1 if expect else 0)

    def test_trend_up_4h_boundary(self, s3m):
        w = windows(w15m(), w1h(chg=1.0),
                    h4={'chg': 1.99, 'ema20': 100.0, 'ema60': 100.0,
                         'atr_pct': 0.05, 'high': 101.0, 'low': 99.0,
                         'volatility': 0.0, 'close': 100.0})
        assert 'TREND_UP' not in types(detect(s3m, w))

    @pytest.mark.parametrize('chg24,ema20,ema60,expect', [
        (4.99, 101.0, 100.0, False), (5.0, 101.0, 100.0, True),
        (5.0, 100.0, 101.0, False)])                  # ema20>ema60 严格
    def test_trend_up_24h_branch(self, s3m, chg24, ema20, ema60, expect):
        w = windows(w15m(), d24=({'chg': chg24, 'ema20': ema20,
                                   'ema60': ema60}))
        assert types(detect(s3m, w)).count('TREND_UP') == (1 if expect else 0)

    def test_trend_down_mirror(self, s3m):
        w = windows(w15m(), w1h(chg=-1.0),
                    h4={'chg': -2.0, 'ema20': 100.0, 'ema60': 100.0,
                         'atr_pct': 0.05, 'high': 101.0, 'low': 99.0,
                         'volatility': 0.0, 'close': 100.0})
        evs = detect(s3m, w)
        assert types(evs) == ['TREND_DOWN']
        assert evs[0]['strength'] == 20                    # |1*10+2*5|=20 ≥15

    def test_trend_up_strength_no_floor15_when_negative_sum(self, s3m):
        """TREND_UP 公式无 abs：1h*10+4h*5 可为 15 边界（1h=1,4h=2 → 20）。"""
        w = windows(w15m(), w1h(chg=1.0),
                    h4={'chg': 2.0, 'ema20': 100.0, 'ema60': 100.0,
                         'atr_pct': 0.05, 'high': 101.0, 'low': 99.0,
                         'volatility': 0.0, 'close': 100.0})
        evs = detect(s3m, w)
        up = [e for e in evs if e['type'] == 'TREND_UP'][0]
        assert up['strength'] == 20


# ──HIGH_VOL / LOW_VOL / ATR_EXPAND ───────────────────────────────────

class TestVolRegime:
    def test_high_vol_boundary_strength(self, s3m):
        evs = detect(s3m, windows(w15m(vol_ratio=2.0)))
        assert types(evs) == ['HIGH_VOL']
        assert evs[0]['strength'] == 30                    # int(2.0*15)=30
        assert 'side' not in evs[0]

    def test_low_vol_boundary_exclusive_of_zero(self, s3m):
        w = windows(w15m(vol_ratio=0.3))
        assert types(detect(s3m, w)) == ['LOW_VOL']
        w = windows(w15m(vol_ratio=0.0))                   # ratio=0 → 不发 LOW
        assert types(detect(s3m, w)) == []

    def test_high_and_low_coexist_unchanged_order(self, s3m):
        """vol_ratio 2.0 只发 HIGH；0.3 只发 LOW；两者不同窗口参数不叠发。"""
        eva = detect(s3m, windows(w15m(vol_ratio=2.0)))
        assert types(eva) == ['HIGH_VOL']
        evb = detect(s3m, windows(w15m(vol_ratio=0.3)))
        assert types(evb) == ['LOW_VOL']

    def test_atr_expand_strict_gt_and_floor_none(self, s3m):
        w = windows(w15m(atr_pct=1.0),
                    w1h(atr_pct=0.5))
        evs = detect(s3m, w)
        # STRICT > 2×: 1.0 > 1.0 False
        assert 'ATR_EXPAND' not in types(evs)
        w = windows(w15m(atr_pct=1.01), w1h(atr_pct=0.5))
        evs = detect(s3m, w)
        assert 'ATR_EXPAND' in types(evs)
        e = [x for x in evs if x['type'] == 'ATR_EXPAND'][0]
        assert e['strength'] == 30                         # int(1.01*30)=30
        assert e['atr_15m'] == 1.01 and e['atr_1h'] == 0.5


# ── 复合多事件 + 顺序 ─────────────────────────────────────────────────

class TestOrderingAndCompound:
    def test_no_side_event_any(self, s3m):
        """确认所有事件均缺 'side' 字段（FAILED_BREAKOUT 例外仅 direction）。"""
        for chg, vol in [(6.0, 2.0), (-6.0, 2.0), (-3.99, 3.0)]:
            evs = detect(s3m, windows(w15m(chg=chg, vol_ratio=vol)))
            assert all('side' not in e for e in evs)

    def test_detection_order_frozen(self, s3m):
        """同参数下事件按源码顺序 append（非 strength 排序——排序仅在写层）。"""
        evs = detect(s3m, windows(w15m(chg=-5.0, vol_ratio=2.5)))
        assert types(evs) == ['PULSE_DOWN', 'PANIC_SELL', 'HIGH_VOL']
