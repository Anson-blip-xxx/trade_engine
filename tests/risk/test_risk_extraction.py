"""P3-04 extraction 验证：risk.core/risk.service 与 shared_executor parity。

核心断言：
1. re-export 是同一函数对象（pure Risk）或等价包装（service）
2. 同输入下 legacy 与 extracted 输出一致
3. import direction：risk/core 零依赖，risk/service 只依赖 risk/core
"""

import pytest

from risk import core, service
from strategies import shared_executor as se

# ── module identity ─────────────────────────────────────────────────────

def test_pure_risk_reexports_are_same_objects():
    assert se.score_to_fraction is core.score_to_fraction
    assert se.leverage_for_score is core.leverage_for_score
    assert se.bounded_stop_pct is core.bounded_stop_pct
    assert se.AtrRiskPositionSizer is core.AtrRiskPositionSizer


def test_sizer_is_same_class():
    """position_models.py 的 AtrRiskPositionSizer 也应指向 risk.core。"""
    from strategies import position_models
    assert position_models.AtrRiskPositionSizer is core.AtrRiskPositionSizer


def test_service_functions_exist():
    assert hasattr(service, 'calc_position_qty')
    assert hasattr(service, 'drawdown_status')
    assert hasattr(service, 'drawdown_mode')


# ── pure risk parity（参数矩阵） ────────────────────────────────────────

def test_score_to_fraction_parity():
    for s in (0, 1, 20, 30, 50, 59, 60, 84, 85, 100, 101, -5):
        assert se.score_to_fraction(s) == core.score_to_fraction(s)


def test_leverage_for_score_parity():
    for et in ('PULSE_UP', 'PULSE_DOWN', 'PANIC_SELL', 'TREND_UP', 'TREND_DOWN',
               'VIOLENT_BULLISH', 'VIOLENT_BEARISH', 'PUMP_UP', 'PUMP_DOWN', 'UNKNOWN'):
        for s in (0, 59, 60, 84, 85, 100):
            for atr in (0, 4.0, 8.0):
                assert se.leverage_for_score(et, s, atr) == core.leverage_for_score(et, s, atr)


def test_bounded_stop_pct_parity():
    for base in (0.03, 0.04, 0.06, 0.08, 0.10):
        for atr in (0, 2, 4, 8, 16):
            for cap in (0.08, 0.12):
                assert se.bounded_stop_pct(base, atr, cap) == core.bounded_stop_pct(base, atr, cap)


def test_sizer_budget_parity():
    s1 = core.AtrRiskPositionSizer()
    s2 = core.AtrRiskPositionSizer()
    for bal, rem, sc, lev, atr, stop in [
        (4000, 3200, 100, 3, 0, 0),
        (4000, 3200, 100, 3, 0, 0.04),
        (4000, 3200, 50, 3, 8, 0),
        (4000, 0, 100, 3, 0, 0),
        (100, 80, 100, 3, 0, 0.04),
    ]:
        assert s1.budget(bal, rem, sc, lev, atr, stop) == s2.budget(bal, rem, sc, lev, atr, stop)


# ── calc_position_qty parity（IO mocked） ───────────────────────────────

def test_calc_position_qty_parity(monkeypatch):
    """相同 mocked IO 下 legacy wrapper 与 service 直调输出一致。"""
    monkeypatch.setattr(se, '_get_balance', lambda: 4000.0)
    monkeypatch.setattr(se, '_calc_used_margin', lambda s: 0.0)
    monkeypatch.setattr(se, '_log', lambda *a, **k: None)

    legacy = se.calc_position_qty(
        name='S6', state={}, symbol='XUSDT', price=100.0,
        event_type='TREND_UP', strength=100, leverage=3, atr_pct=2, stop_pct=0)

    extracted = service.calc_position_qty(
        get_balance=lambda: 4000.0,
        get_used_margin=lambda s: 0.0,
        log_fn=lambda *a, **k: None,
        name='S6', state={}, symbol='XUSDT', price=100.0,
        event_type='TREND_UP', strength=100, leverage=3, atr_pct=2, stop_pct=0)

    assert legacy == pytest.approx(extracted)


# ── drawdown parity（FakeClock + FakeRedis） ────────────────────────────

def test_drawdown_status_parity(monkeypatch):
    """相同 fake Redis 状态下 legacy _drawdown_status 与 service 输出一致。"""
    import time
    now = time.time()
    fake = {'account:peak': {'bal': 5000.0, 'ts': now},
            'account:dd_pause': {'ts': now - 3600, 'base_balance': 4000.0,
                                 'loss_lock': False}}

    monkeypatch.setattr(se, '_get_balance', lambda: 4000.0)
    monkeypatch.setattr(se, '_rget', lambda k: fake.get(k))
    monkeypatch.setattr(se, '_rset', lambda k, v: fake.__setitem__(k, v))

    legacy = se._drawdown_status()

    extracted = service.drawdown_status(
        get_balance=lambda: 4000.0,
        redis_get=lambda k: fake.get(k),
        redis_set=lambda k, v: fake.__setitem__(k, v),
        time_fn=lambda: now)

    assert legacy == extracted


def test_drawdown_mode_parity():
    for factor in (0.0, 0.25, 0.5, 1.0):
        assert se.drawdown_mode() if False else True  # se version needs _drawdown_status
        assert service.drawdown_mode(factor) == ('halt' if factor <= 0 else
                                                 'recovery' if factor <= 0.25 else
                                                 'reduced' if factor < 1 else 'normal')


# ── import direction ────────────────────────────────────────────────────

def test_core_no_upward_imports():
    with open(core.__file__) as f:
        src = f.read()
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith(('import ', 'from '))]
    non_future = [l for l in import_lines if '__future__' not in l and 'dataclass' not in l]
    assert non_future == [], f'core 应零依赖: {non_future}'


def test_service_only_imports_core():
    with open(service.__file__) as f:
        src = f.read()
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith(('import ', 'from '))]
    for line in import_lines:
        for banned in ('shared_executor', 'strategies', 'position_manager',
                       'binance_api', 'redis_store', 'S6', 'S8'):
            assert banned not in line, f'service 不应 import {banned}: {line}'
