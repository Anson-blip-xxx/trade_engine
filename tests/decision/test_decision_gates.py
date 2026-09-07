"""Golden：纯 Decision Gate 函数（当前行为锁定，非"正确性"判断）。

全部走真实 legacy 实现；期望值来自探针实测（见 P3-00 Inventory）。
"""
import time

import pytest

from strategies.position_models import AtrRiskPositionSizer
from strategies.shared_executor import (
    bounded_stop_pct,
    classify_entry_mode,
    event_age_sec,
    event_is_stale,
    leverage_for_score,
    long_signal_allows_open,
    long_trend_takeover_ready,
    price_is_overextended,
    pump_down_uptrend_guard,
    score_to_fraction,
    short_signal_allows_open,
)

NOW = time.time()


# ── event_is_stale / event_age_sec ──────────────────────────────────────

def test_event_is_stale_within_120s():
    assert event_is_stale({'ts': NOW - 100}) is False


def test_event_is_stale_after_120s():
    assert event_is_stale({'ts': NOW - 130}) is True


def test_event_is_stale_ms_timestamp():
    """毫秒时间戳自动归一（>10^12 视为 ms）。"""
    assert event_is_stale({'ts': (NOW - 100) * 1000}) is False
    assert event_is_stale({'ts': (NOW - 130) * 1000}) is True


def test_event_age_sec_plain_event_near_zero():
    """无 ts 的事件 → age ≈ 0（两次 time.time() 差）。"""
    assert event_age_sec({'symbol': 'X'}) < 1.0


# ── classify_entry_mode ─────────────────────────────────────────────────

def test_classify_long_matrix():
    """LONG：price>=ema20→RIGHT；price<ema20+rsi<=35+flow→LEFT；否则 UNCONFIRMED。"""
    assert classify_entry_mode(100.0, 100.0, 50, None, 'LONG') == 'RIGHT_MOMENTUM'
    assert classify_entry_mode(98.0, 100.0, 30, None, 'LONG') == 'LEFT_REVERSAL'
    assert classify_entry_mode(98.0, 100.0, 36, None, 'LONG') == 'UNCONFIRMED'


def test_classify_long_rsi_boundary_35():
    """rsi==35 → LEFT（<=35）。"""
    assert classify_entry_mode(98.0, 100.0, 35, None, 'LONG') == 'LEFT_REVERSAL'


def test_classify_long_taker_boundary():
    """taker 0.52 → LEFT（>=0.52）；0.51 → UNCONFIRMED。"""
    assert classify_entry_mode(98.0, 100.0, 30, 0.52, 'LONG') == 'LEFT_REVERSAL'
    assert classify_entry_mode(98.0, 100.0, 30, 0.51, 'LONG') == 'UNCONFIRMED'


def test_classify_short_matrix():
    assert classify_entry_mode(100.0, 100.0, 50, None, 'SHORT') == 'RIGHT_MOMENTUM'
    assert classify_entry_mode(102.0, 100.0, 70, None, 'SHORT') == 'LEFT_REVERSAL'
    assert classify_entry_mode(102.0, 100.0, 64, None, 'SHORT') == 'UNCONFIRMED'


def test_classify_short_rsi_boundary_65():
    assert classify_entry_mode(102.0, 100.0, 65, None, 'SHORT') == 'LEFT_REVERSAL'


def test_classify_short_taker_boundary():
    """taker 0.48 → LEFT（<=0.48）；0.49 → UNCONFIRMED。"""
    assert classify_entry_mode(102.0, 100.0, 70, 0.48, 'SHORT') == 'LEFT_REVERSAL'
    assert classify_entry_mode(102.0, 100.0, 70, 0.49, 'SHORT') == 'UNCONFIRMED'


def test_classify_price_equals_ema_is_right_momentum():
    """price == ema20 → RIGHT_MOMENTUM（两侧同界）。"""
    assert classify_entry_mode(100.0, 100.0, 50, None, 'LONG') == 'RIGHT_MOMENTUM'
    assert classify_entry_mode(100.0, 100.0, 50, None, 'SHORT') == 'RIGHT_MOMENTUM'


# ── price_is_overextended ───────────────────────────────────────────────

def test_overextended_long_boundary():
    """ext == max_atr → 未超（严格 >）。"""
    assert price_is_overextended(102.0, 100.0, 1.0, 'LONG', 2.0) is False
    assert price_is_overextended(102.1, 100.0, 1.0, 'LONG', 2.0) is True


def test_overextended_short_boundary():
    assert price_is_overextended(98.0, 100.0, 1.0, 'SHORT', 2.0) is False
    assert price_is_overextended(97.9, 100.0, 1.0, 'SHORT', 2.0) is True


def test_overextended_zero_atr_guard():
    """atr=0 → False（防除零 guard 优先）。"""
    assert price_is_overextended(102.0, 100.0, 0.0, 'LONG', 2.0) is False


# ── signal allows_open ──────────────────────────────────────────────────

def test_long_signal_allows_open_regimes():
    """VIOLENT_BULLISH × 空头 regime → 拒绝。"""
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'weak_bear'}) is False
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'risk-off'}) is False
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'risk_off'}) is False
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'range'}) is True
    assert long_signal_allows_open('VIOLENT_BULLISH', {'regime': 'weak_bull'}) is True


def test_long_signal_non_violent_always_true():
    """非 VIOLENT_BULLISH 不受 regime 限制。"""
    for et in ('TREND_UP', 'PULSE_UP', 'PUMP_UP'):
        assert long_signal_allows_open(et, {'regime': 'risk-off'}) is True


def test_short_signal_strength_boundary_59_60_61():
    """确认型短空 59 拒 / 60 过 / 61 过（锁定边界）。"""
    assert short_signal_allows_open('TREND_DOWN', 59) is False
    assert short_signal_allows_open('TREND_DOWN', 60) is True
    assert short_signal_allows_open('TREND_DOWN', 61) is True
    assert short_signal_allows_open('VIOLENT_BEARISH', 59) is False
    assert short_signal_allows_open('PULSE_DOWN', 60) is True


def test_short_signal_panic_pump_exempt():
    """PANIC_SELL / PUMP_DOWN 无强度门槛。"""
    assert short_signal_allows_open('PANIC_SELL', 10) is True
    assert short_signal_allows_open('PUMP_DOWN', 10) is True


# ── leverage_for_score ──────────────────────────────────────────────────

def test_leverage_pulse_score_boundary_59_60():
    """PULSE_UP base 5：score<60 → 2；>=60 → 3（<85 且 atr<4）。"""
    assert leverage_for_score('PULSE_UP', 59) == 2
    assert leverage_for_score('PULSE_UP', 60) == 3


def test_leverage_pulse_score_boundary_84_85():
    """score>=85 → base 5。"""
    assert leverage_for_score('PULSE_UP', 84) == 3
    assert leverage_for_score('PULSE_UP', 85) == 5


def test_leverage_atr_boundary_4():
    """atr>=4 → 杠杆压到 3（即使 score 100）。"""
    assert leverage_for_score('PULSE_UP', 100, 4) == 3
    assert leverage_for_score('PULSE_UP', 100, 4.01) == 3
    assert leverage_for_score('PULSE_UP', 100, 3.99) == 5


def test_leverage_trend_capped_at_3():
    """TREND base 3：永远不超过 3。"""
    assert leverage_for_score('TREND_UP', 59) == 2
    assert leverage_for_score('TREND_UP', 60) == 3
    assert leverage_for_score('TREND_UP', 85) == 3
    assert leverage_for_score('TREND_DOWN', 100) == 3


def test_leverage_unknown_event_defaults_3():
    assert leverage_for_score('UNKNOWN_EVENT', 100) == 3


# ── long_trend_takeover_ready ───────────────────────────────────────────

def _takeover_market(**over):
    m = {'15m': {'ema20': 99.5, 'atr': 0.5, 'taker_buy_ratio': 0.60},
         '4h': {'ema20': 98.0, 'chg': 5.0},
         '24h': {'ema20': 95.0, 'ema60': 90.0, 'chg': 15.0}}
    m.update(over)
    return m


def test_takeover_ready_happy():
    assert long_trend_takeover_ready(100.0, _takeover_market()) is True


def test_takeover_flow_below_052_fails():
    m = _takeover_market()
    m['15m']['taker_buy_ratio'] = 0.50
    assert long_trend_takeover_ready(100.0, m) is False


def test_takeover_chg4h_negative_fails():
    m = _takeover_market()
    m['4h']['chg'] = -1.0
    assert long_trend_takeover_ready(100.0, m) is False


def test_takeover_distance_far_fails():
    """价格距 15m EMA20 太远 → False。"""
    assert long_trend_takeover_ready(105.0, _takeover_market()) is False


def test_takeover_price_not_above_ema24_fails():
    m = _takeover_market()
    m['24h']['ema20'] = 105.0
    assert long_trend_takeover_ready(100.0, m) is False


# ── pump_down_uptrend_guard ─────────────────────────────────────────────

def test_pump_guard_true_when_uptrend():
    """价格在 4h EMA20 上方 + 4h/24h 均上涨 → guard 触发。"""
    assert pump_down_uptrend_guard(100.0, {'ema20': 90.0, 'chg': 5.0},
                                   {'chg': 20.0}) is True


def test_pump_guard_false_when_price_below_ema4h():
    assert pump_down_uptrend_guard(85.0, {'ema20': 90.0, 'chg': 5.0},
                                   {'chg': 20.0}) is False


def test_pump_guard_false_when_chg24h_below_15():
    assert pump_down_uptrend_guard(100.0, {'ema20': 90.0, 'chg': 5.0},
                                   {'chg': 10.0}) is False


# ── score_to_fraction / AtrRiskPositionSizer / bounded_stop_pct ─────────

def test_score_to_fraction_clamp():
    """0→3%（下限）；100→15%（上限）；50→7.5%。"""
    assert score_to_fraction(0) == pytest.approx(0.03)
    assert score_to_fraction(50) == pytest.approx(0.075)
    assert score_to_fraction(100) == pytest.approx(0.15)


def test_sizer_budget_risk_cap():
    """风险帽：stop 4% × lev 3 → 仓位 ≤ 余额 1% / (lev×stop)。"""
    sizer = AtrRiskPositionSizer()
    budget = sizer.budget(4000, 3200, 100, 3, 0, 0.04)
    assert budget == pytest.approx(4000 * 0.01 / (3 * 0.04))


def test_sizer_budget_min_notional_floor():
    """极端小 remaining → min_notional 10 兜底。"""
    sizer = AtrRiskPositionSizer()
    assert sizer.budget(4000, 1, 0, 3, 0, 0) == pytest.approx(10.0)


def test_bounded_stop_pct_atr_expansion_and_caps():
    """冻结四象限：固定优先 / ATR 放大 / 8% cap / takeover 12% cap。"""
    assert bounded_stop_pct(0.08, 2.0, 0.08) == pytest.approx(0.08)
    assert bounded_stop_pct(0.04, 5.0, 0.08) == pytest.approx(0.08)
    assert bounded_stop_pct(0.10, 0.0, 0.08) == pytest.approx(0.08)
    assert bounded_stop_pct(0.04, 9.0, 0.12) == pytest.approx(0.12)
