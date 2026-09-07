"""Phase 3-02 extraction 验证：core 与 compatibility API 输出一致。

核心断言：同一输入下 decision.core 与 strategies.shared_executor 的
同名函数返回完全相同的结果（因为 re-export 是同一函数对象）。
另外验证 module identity / import direction / 无循环依赖。
"""

from decision import core
from strategies import shared_executor as se

_M = {'15m': {'ema20': 99.5, 'atr': 0.5, 'taker_buy_ratio': 0.60},
      '4h': {'ema20': 98.0, 'chg': 5.0},
      '24h': {'ema20': 95.0, 'ema60': 90.0, 'chg': 15.0}}


# ── module identity：re-export 是同一函数对象 ────────────────────────────

def test_re_exports_are_same_objects():
    """shared_executor 的函数名直接引用 decision.core 的函数对象。"""
    assert se.contract_score is core.contract_score
    assert se.classify_entry_mode is core.classify_entry_mode
    assert se.price_is_overextended is core.price_is_overextended
    assert se.long_signal_allows_open is core.long_signal_allows_open
    assert se.short_signal_allows_open is core.short_signal_allows_open
    assert se.long_trend_takeover_ready is core.long_trend_takeover_ready
    assert se.pump_down_uptrend_guard is core.pump_down_uptrend_guard
    assert se.resolve_event_flow is core.resolve_event_flow
    assert se.resolve_event_orderflow_bias is core.resolve_event_orderflow_bias
    assert se.MIN_CONFIRMED_SHORT_STRENGTH == core.MIN_CONFIRMED_SHORT_STRENGTH
    assert se.CONFIRMED_SHORT_SIGNALS == core.CONFIRMED_SHORT_SIGNALS


# ── 行为一致性（同输入 → 同输出） ────────────────────────────────────────

def test_contract_score_same():
    for strength in (0, 10, 30, 50, 70, 99, 100):
        for atr in (0, 2, 5, 9, 20):
            for ext in (0, 1.0, 5.0):
                for flow in (None, 0.52, 0.48):
                    for side in ('LONG', 'SHORT'):
                        assert se.contract_score(
                            strength, 'TREND_UP', atr, ext, flow, 0, side, None
                        ) == core.contract_score(
                            strength, 'TREND_UP', atr, ext, flow, 0, side, None)


def test_classify_entry_mode_same():
    for price in (98.0, 100.0, 102.0):
        for rsi in (30, 35, 36, 50, 64, 65, 70):
            for taker in (None, 0.48, 0.52):
                for side in ('LONG', 'SHORT'):
                    assert se.classify_entry_mode(
                        price, 100.0, rsi, taker, side
                    ) == core.classify_entry_mode(
                        price, 100.0, rsi, taker, side)


def test_price_is_overextended_same():
    for price in (98.0, 100.0, 102.1):
        for side in ('LONG', 'SHORT'):
            for max_atr in (1.0, 2.0):
                assert se.price_is_overextended(
                    price, 100.0, 1.0, side, max_atr
                ) == core.price_is_overextended(
                    price, 100.0, 1.0, side, max_atr)


def test_signal_gates_same():
    for et in ('TREND_UP', 'VIOLENT_BULLISH', 'PUMP_UP'):
        for regime in ('range', 'weak_bear', 'risk-off', 'risk_off'):
            assert se.long_signal_allows_open(
                et, {'regime': regime}) == core.long_signal_allows_open(
                et, {'regime': regime})
    for et in ('TREND_DOWN', 'PANIC_SELL', 'PUMP_DOWN', 'VIOLENT_BEARISH'):
        for s in (30, 59, 60, 61, 99):
            assert se.short_signal_allows_open(et, s) == core.short_signal_allows_open(et, s)


def test_takeover_ready_same():
    for price in (95.0, 100.0, 105.0):
        assert se.long_trend_takeover_ready(price, _M) == core.long_trend_takeover_ready(price, _M)


def test_pump_guard_same():
    for price in (85.0, 100.0):
        for chg4 in (-1.0, 5.0):
            for chg24 in (10.0, 20.0):
                w4h = {'ema20': 90.0, 'chg': chg4}
                w24h = {'chg': chg24}
                assert se.pump_down_uptrend_guard(price, w4h, w24h) == \
                    core.pump_down_uptrend_guard(price, w4h, w24h)


def test_resolve_flow_same():
    evt = {'taker_buy_ratio': 0.55}
    mkt = {'15m': {'taker_buy_ratio': 0.40}}
    assert se.resolve_event_flow(evt, mkt) == core.resolve_event_flow(evt, mkt)
    assert se.resolve_event_flow({}, mkt) == core.resolve_event_flow({}, mkt)


def test_resolve_orderflow_bias_same():
    evt = {'orderflow_bias': -0.3}
    mkt = {'15m': {'orderflow_bias': -1.0}}
    assert se.resolve_event_orderflow_bias(evt, mkt) == \
        core.resolve_event_orderflow_bias(evt, mkt)
    assert se.resolve_event_orderflow_bias({}, mkt) == \
        core.resolve_event_orderflow_bias({}, mkt)


# ── import direction：core 不依赖上层模块 ────────────────────────────────

def test_core_has_no_upward_imports():
    """decision/core.py 不得 import shared_executor/S6/S8/PM/Binance/Redis。"""
    with open(core.__file__) as f:
        src = f.read()
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith(('import ', 'from '))]
    for line in import_lines:
        for banned in ('shared_executor', 'strategies', 'position_manager',
                       'binance_api', 'redis_store', 'S6', 'S8',
                       'clickhouse'):
            assert banned not in line, f'decision/core.py 不应 import {banned}: {line}'


def test_core_only_uses_stdlib():
    """decision/core.py 的 import 应只有 __future__。"""
    with open(core.__file__) as f:
        src = f.read()
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith('import ') or l.strip().startswith('from ')]
    non_future = [l for l in import_lines if '__future__' not in l]
    assert non_future == [], f'decision/core.py 应零依赖（除 __future__），发现: {non_future}'


def test_no_circular_import():
    """import decision.core 后 shared_executor 仍可导入（无循环）。"""
    assert hasattr(se, 'contract_score')
    assert core.contract_score is not None
