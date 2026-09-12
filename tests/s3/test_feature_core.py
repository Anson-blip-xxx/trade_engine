"""P5-02：S3 Feature Core parity —— legacy（真实函数）vs s3.core 对拍。

场景 A-J（prompt 十节）：normal/flat/rising/falling/high-vol/low-vol/
short history/zero volume/extreme taker/malformed-but-supported boundary。
数值 assert 精确相等（legacy float 语义 → 不使用 approx）。
"""
import pytest

import strategies.s3_orderflow as s3
from s3 import core


def kc(t, o, h, l, c, v, tbv=0.0):
    return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v, 'tbv': tbv}


def candles_from_closes(closes, *, vol=10.0, tbv=None):
    """构造最新→最旧序列：closes[0]=最新；tr 取相邻 candle 差。"""
    out = []
    n = len(closes)
    for idx, c in enumerate(closes):          # idx 0=最新
        prev_c = closes[idx + 1] if idx + 1 < n else c
        h = max(c, prev_c) * 1.002
        l = min(c, prev_c) * 0.998
        out.append(kc(n - idx, c, h, l, c, vol, tbv if tbv is not None else vol * 0.5))
    return out  # newest→oldest


def reversed_candles(candles):
    return list(reversed(candles))


def window_parities(s3m, closes, vol=10.0, tbv=None):
    candles = candles_from_closes(closes, vol=vol, tbv=tbv)
    window_min = len(candles)
    a = s3m.compute_window_data(list(candles), window_min, 'PARITYSYM')
    b = core.build_window_features(reversed_candles(candles),
                                   ema20=s3m.compute_ema(
                                       [c['c'] for c in reversed(candles)],
                                       20, 'CORESYM'),
                                   ema60=s3m.compute_ema(
                                       [c['c'] for c in reversed(candles)],
                                       60, 'CORESYM'))
    return a, b


# ═══════════════════════════════════════════════════════════════
# A. normal market
# ═══════════════════════════════════════════════════════════════

def test_normal_market_window_parity(s3m):
    closes = [100.0 - 0.05 * i for i in range(20)]           # 微跌序列（最新=100）
    a, b = window_parities(s3m, closes, vol=10.0)
    assert list(a.keys()) == list(b.keys())                   # 键序原位
    assert a == b


# ═══════════════════════════════════════════════════════════════
# B. flat market
# ═══════════════════════════════════════════════════════════════

def test_flat_market(s3m):
    candles = candles_from_closes([100.0] * 30, vol=10.0)
    a = s3m.compute_window_data(list(candles), 30, 'S1')
    b = core.build_window_features(reversed_candles(candles),
                                   s3m.compute_ema([c['c'] for c in reversed(candles)], 20, 'C1'),
                                   s3m.compute_ema([c['c'] for c in reversed(candles)], 60, 'C1'))
    assert a['chg'] == 0.0 and b['chg'] == 0.0
    assert a['vol_ratio'] == b['vol_ratio'] == 1.0
    assert a['atr'] == b['atr']
    assert a == b
    # flat close 但 h/l 带 helper 的 ±0.2% 波动 → drawdown<0 / volatility>0（parity 保持）
    assert a['drawdown'] <= 0.0
    assert a['volatility'] == b['volatility']


# ═══════════════════════════════════════════════════════════════
# C. rising market
# ═══════════════════════════════════════════════════════════════

def test_rising_market_parity(s3m):
    closes = [100.0 * (1 + 0.001 * (39 - i)) for i in range(40)]  # 最新最大
    a, b = window_parities(s3m, closes, vol=10.0)
    assert a == b
    assert a['chg'] > 0


# ═══════════════════════════════════════════════════════════════
# D. falling market
# ═══════════════════════════════════════════════════════════════

def test_falling_market_parity(s3m):
    closes = [100.0 / (1 + 0.001 * (39 - i)) for i in range(40)]  # 最新最小
    a, b = window_parities(s3m, closes, vol=10.0)
    assert a == b
    assert a['chg'] < 0


# ═══════════════════════════════════════════════════════════════
# E. high volatility
# ═══════════════════════════════════════════════════════════════

def test_high_volatility_parity(s3m):
    closes = [100.0, 108.0, 90.0, 110.0, 88.0, 105.0, 92.0, 112.0,
              90.0, 120.0]
    a, b = window_parities(s3m, closes, vol=100.0)
    assert a == b
    assert a['volatility'] > 20.0
    assert a['drawdown'] <= 0.0              # (minlow - maxhigh)/maxhigh


# ═══════════════════════════════════════════════════════════════
# F. low volatility
# ═══════════════════════════════════════════════════════════════

def test_low_volatility_parity(s3m):
    closes = [100.0 - 0.001 * (19 - i) for i in range(20)]
    a, b = window_parities(s3m, closes, vol=10.0)
    assert a == b
    assert a['volatility'] < 0.5  # helper 自带的 ±0.2% h/l 抖动


# ═══════════════════════════════════════════════════════════════
# G. short history
# ═══════════════════════════════════════════════════════════════

def test_short_history_fallbacks_parity(s3m):
    closes = [102.0, 101.0, 100.0]  # closes[0]=最新；3 根 < 20/60
    a, b = window_parities(s3m, closes)
    assert a == b
    assert a['ema20'] == b['ema20'] == 102.0    # <period → values[-1] = 最新（反转数组内）
    assert a['ema60'] == b['ema60'] == 102.0


def test_empty_window_fallback(s3m):
    assert s3m.compute_window_data([], 15, '') == {}   # 保留 legacy 语义


def test_atr_single_candle_zero(s3m):
    # legacy = core
    candles = candles_from_closes([100.0])
    assert s3m.compute_atr(candles) == core.atr(list(reversed(candles))) == 0


# ═══════════════════════════════════════════════════════════════
# H. zero volume
# ═══════════════════════════════════════════════════════════════

def test_zero_volume_parity(s3m):
    closes = [100.0 - 0.05 * i for i in range(20)]
    a, b = window_parities(s3m, closes, vol=0.0)
    assert a == b
    # avg_vol=0 → vol_ratio default 1.0（守卫）
    assert a['vol_ratio'] == 1.0

def test_all_zero_tbv_parity(s3m):
    closes = [101.0, 99.0, 100.0]              # closes[0]=最新
    candles = candles_from_closes(closes, vol=0, tbv=0.0)
    a, b = window_parities(s3m, closes, vol=0, tbv=0.0)
    assert a == b


# ═══════════════════════════════════════════════════════════════
# I. extreme taker buy
# ═══════════════════════════════════════════════════════════════

def test_all_taker_buy_parity(s3m):
    """taker_buy == volume → taker_sell clamped 0；ratio=1.0。"""
    candles = candles_from_closes([100.0, 99.5, 101.0, 100.0], vol=10.0,
                                  tbv=10.0)
    a, b = window_parities(s3m, [100.0, 99.5, 101.0, 100.0], vol=10.0, tbv=10.0)
    assert a['taker_buy_ratio'] == b['taker_buy_ratio'] == 1.0
    assert a['taker_sell_volume'] == b['taker_sell_volume'] == 0.0
    assert a['orderflow_bias'] == b['orderflow_bias'] == 1.0
    assert a == b


def test_taker_zero_parity(s3m):
    a, b = window_parities(s3m, [100.0, 99.5], vol=10.0, tbv=0.0)
    assert a['taker_buy_ratio'] == b['taker_buy_ratio'] == 0.0
    assert a == b


# ═══════════════════════════════════════════════════════════════
# J. malformed-but-currently-supported boundaries
# ═══════════════════════════════════════════════════════════════

def test_missing_tbv_default_0(s3m):
    candles = [{'t': i, 'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 10}
               for i in range(5)]              # 无 tbv 键
    candles = sorted(candles, key=lambda x: -x['t'])
    a = s3m.compute_window_data(candles, 5, 'NOTBVIDX')
    closes = [100.0] * 5
    rev_candles = list(reversed(candles))
    b = core.build_window_features(rev_candles,
                                   ema20=core.ema([c['c'] for c in rev_candles], 20),
                                   ema60=core.ema([c['c'] for c in rev_candles], 60))
    assert a['taker_buy_ratio'] == b['taker_buy_ratio'] == 0.0
    assert a == b


def test_zero_first_close_fallback(s3m):
    closes = [105.0, 104.0, 103.0, 0.0]        # closes[-1]=最旧=0 → first_close=0
    a, b = window_parities(s3m, closes)
    assert a == b
    assert a['chg'] == 0.0                     # first_close falsy → chg=0")

    closes = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_negative_high_drawdown_fallback(s3m):
    """max(highs)>0 守卫下负价序列的实际行为冻结（不改善）。"""
    closes = [-1.0, -2.0, -3.0, -5.0]          # closes[-1]=最旧
    a, b = window_parities(s3m, closes)
    assert a == b


# ═══════════════════════════════════════════════════════════════
#  Numeric detail parity
# ═══════════════════════════════════════════════════════════════

class TestAtrParity:
    def test_normal_atr_exact(self, s3m):
        candles = [
            {'t': 3, 'o': 100, 'h': 106, 'l': 98, 'c': 104},
            {'t': 2, 'o': 98, 'h': 102, 'l': 96, 'c': 99},
            {'t': 1, 'o': 98, 'h': 104, 'l': 95, 'c': 100},
        ]
        # legacy (delegating) == core
        assert s3m.compute_atr(candles) == core.atr(candles)

    def test_gap_high_atr(self, s3m):
        candles = [
            {'t': 3, 'o': 100, 'h': 120, 'l': 99, 'c': 119},
            {'t': 2, 'o': 100, 'h': 101, 'l': 95, 'c': 100},
            {'t': 1, 'o': 98, 'h': 98, 'l': 94, 'c': 96},
        ]
        assert s3m.compute_atr(candles) == core.atr(candles)

    def test_period_clipping(self, s3m):
        candles = candles_from_closes([100.0 - 0.1 * i for i in range(40)],
                                      vol=5)
        assert s3m.compute_atr(candles, period=5) == core.atr(candles, 5)

    def test_zero_figure_same_time(self, s3m):
        candles = candles_from_closes([100.0, 100.0, 100.0])
        assert s3m.compute_atr(candles) == core.atr(candles) >= 0.0


class TestRsiParity:
    def test_all_gains_exact_100(self, s3m):
        asc = [float(i) for i in range(21)]
        assert s3m.compute_rsi(asc) == core.rsi(asc) == 100.0

    def test_all_losses_exact_0(self, s3m):
        desc = [float(-i) for i in range(21)]
        assert s3m.compute_rsi(desc) == core.rsi(desc) == 0.0

    def test_mixed_series_parity(self, s3m):
        import random
        seq = [100.0, 101.5, 99.0, 100.3, 102.0, 98.0, 100.0, 101.0,
               99.5, 100.2, 103.0, 99.0, 102.4, 98.2, 100.0, 101.2, 97.0,
               100.2, 99.2, 102.0, 100.0]
        assert s3m.compute_rsi(seq) == core.rsi(seq)

    def test_short_series_default(self, s3m):
        assert s3m.compute_rsi([1.0, 2.0]) == core.rsi([1.0, 2.0]) == 50.0

    def test_exact_period(self, s3m):
        seq = [float(i) for i in range(16)]     # period 14 → len(period+1)=15
        assert s3m.compute_rsi(seq) == core.rsi(seq) == 100.0


class TestEmaParity:
    def test_full_path_parity_no_cache_clear_fixture(self, s3m):
        vals = [float(i) for i in range(40)]
        legacy_full = s3m.compute_ema(vals, 20)           # no symbol → full
        legacy_sym_first = s3m.compute_ema(vals, 20, 'E1')
        core_val = core.ema(vals, 20)
        assert legacy_full == legacy_sym_first == core_val

    def test_period_exactly(self, s3m):
        vals = [float(i) for i in range(20)]
        assert s3m.compute_ema(vals, 20) == core.ema(vals, 20)

    def test_more_than_period(self, s3m):
        vals = [100.0 - 0.5 * i for i in range(60)]
        assert s3m.compute_ema(vals, 20) == core.ema(vals, 20)

    def test_insufficient_data_fallback(self, s3m):
        vals = [7.0, 8.0, 9.0]
        assert s3m.compute_ema(vals, 20) == core.ema(vals, 20) == 9.0

    def test_empty_values_returns_0(self, s3m):
        assert s3m.compute_ema([], 20) == core.ema([], 20) == 0

    def test_cache_path_recorded_not_extracted(self, s3m):
        """增量路径未抽——kernel 只管全量。冻结事实：返回值不同 canonical。"""
        vals = [float(i) for i in range(30)]
        first = s3m.compute_ema(vals, 20, 'C2')           # 填 cache
        inc = s3m.compute_ema(vals[-1:], 20, 'C2')        # 增量路径（values[0]）
        assert first != inc                                # 两条路径差异（S3-1 延伸事实）
        # core 只复现 full path
        assert core.ema(vals, 20) == first
