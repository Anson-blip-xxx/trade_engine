"""shared_executor 风控回归测试：1% 风险硬约束 / 回撤熔断 / R:R 过滤。"""
import time

import pytest


def test_position_qty_risk_cap(patch_executor):
    """止损亏损 = 名义×stop_pct ≤ 账户 1%：高仓位被砍到风险上限。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)  # 账户 4000 USDT

    # 无风险约束时：池=4000×0.8=3200，已用=0，分配=15%（score=100），ATR 不衰减
    qty_free = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=2, stop_pct=0)
    # 池 3200 × 15% = 480 USDT → qty = 480/1.0×3 = 1440
    assert qty_free == pytest.approx(1440.0)

    # 加风险约束 stop=4%：保证金上限 = 4000×0.01/(3×0.04) = 333.33 USDT
    # qty = 保证金/price×leverage = 333.33×3 = 1000
    qty_capped = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                      strength=100, leverage=3, atr_pct=2, stop_pct=0.04)
    assert qty_capped == pytest.approx(1000.0)
    # 验证止损亏损 = 名义×stop_pct = qty×0.04 ≤ 账户1%：1000×0.04=40 = 4000×1%
    assert qty_capped * 0.04 <= 4000 * 0.01 + 1e-9


def test_atr_decay_reduces_size(patch_executor):
    """ATR 高波动衰减：ATR 8% → 因子 0.5，仓位减半。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)

    qty_atr4 = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=4, stop_pct=0)
    qty_atr8 = se.calc_position_qty('S6', {}, 'XXX', price=1.0, event_type='PULSE_UP',
                                    strength=100, leverage=3, atr_pct=8, stop_pct=0)
    assert qty_atr8 == pytest.approx(qty_atr4 * 0.5)


def test_drawdown_half_size(patch_executor):
    """回撤 ≥8% → 仓位系数 0.5。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4400, 'ts': time.time()})  # 峰 4400，回撤 9.09%

    coeff, dd = se._drawdown_status()
    assert coeff == 0.5
    assert dd > 8


def test_drawdown_pause(patch_executor):
    """回撤 ≥15% → 暂停（系数 0）。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4800, 'ts': time.time()})  # 回撤 16.7%

    coeff, dd = se._drawdown_status()
    assert coeff == 0.0
    assert dd >= 15


def test_drawdown_normal(patch_executor):
    """回撤 <8% → 正常系数 1。"""
    se = patch_executor['se']
    patch_executor['set_balance'](4000)
    se._rset('account:peak', {'bal': 4050, 'ts': time.time()})  # 回撤 1.2%

    coeff, dd = se._drawdown_status()
    assert coeff == 1.0


def test_event_expected_move_rr_support():
    """R:R 预判：PULSE_UP 用 15m 涨幅，TREND 用 1h 涨幅，无数据返回 0（不拦截）。"""
    from strategies.shared_executor import _event_expected_move

    assert _event_expected_move({'type': 'PULSE_UP', 'chg_15m': 3.2}) == pytest.approx(3.2)
    assert _event_expected_move({'type': 'TREND_DOWN', 'chg_1h': -2.5}) == pytest.approx(2.5)
    assert _event_expected_move({'type': 'PUMP_UP', 'chg_15m': None}) == 0.0
    assert _event_expected_move({'type': 'UNKNOWN'}) == 0.0


def test_strength_filter_below_30_blocks_open(patch_executor):
    """strength<30 信号直接拒绝开仓（函数第一步，无副作用）。"""
    se = patch_executor['se']

    result = se.open_position('S6', 'XXX', 'LONG', 1.0, 0.95, 100, 'CROSSED', 3,
                              'PULSE_UP', 25)  # strength=25 < 30
    assert result is False
