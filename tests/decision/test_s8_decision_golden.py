"""Golden：S8 SHORT Decision 全路径 characterization。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "strategies")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
import S8
from decision_helpers import (
    EVENT_PUMP_DOWN,
    EVENT_SHORT,
    MARKET_SHORT,
    S8_GATE_ORDER,
    gate_names,
    patch_s8,
    run_s8,
)

from journal import validate_journal
from journal.recorder import MemoryJournalRecorder


@pytest.fixture
def env(monkeypatch):
    mem = MemoryJournalRecorder()
    calls = patch_s8(monkeypatch, S8, recorder=mem, market=MARKET_SHORT)
    return {'calls': calls, 'mem': mem, 'mp': monkeypatch}


def test_s8_normal_path_full_freeze(env):
    """正常路径：12 Gate 全过 → OPEN，score=66，entry_mode=RIGHT_MOMENTUM。"""
    run_s8(S8, EVENT_SHORT)
    assert len(env['calls']['open']) == 1
    j = env['mem'].records[0]
    assert validate_journal(j) == []
    assert gate_names(j) == S8_GATE_ORDER
    assert all(g.passed for g in j.gates)
    assert j.decision.action == 'OPEN' and j.decision.accepted is True
    assert j.decision.final_score == 66
    assert j.strategy.entry_mode == 'RIGHT_MOMENTUM'
    assert j.signal.side == 'SHORT'
    args = env['calls']['open'][0]['args']
    assert args[0] == 'S8' and args[7] == 3
    assert args[4] == pytest.approx(108.0)               # stop 100*(1+0.08)


def test_s8_stale_event(env):
    env['mp'].setattr(S8, 'event_is_stale', lambda evt: True)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == ['strength', 'fresh']
    assert j.decision.reason == 'event_stale'


def test_s8_signal_cooldown(env):
    env['mp'].setattr(S8, 'is_event_fresh', lambda *a, **k: False)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == ['strength', 'fresh', 'signal_cooldown']
    assert j.decision.reason == 'signal_cooldown'


def test_s8_position_limit(env):
    env['mp'].setattr(S8, 'get_position_count', lambda name: 2)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:4]
    assert j.decision.reason == 'position_limit'


def test_s8_symbol_cooldown(env):
    run_s8(S8, EVENT_SHORT, state={'cooldowns': {'TESTUSDT': 9999999999}})
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:5]
    assert j.decision.reason == 'symbol_cooldown'


def test_s8_market_risk_off(env):
    env['mp'].setattr(S8, 'market_allows_trading', lambda name, side: False)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:6]
    assert j.decision.reason == 'market_disallowed'


def test_s8_existing_position(env):
    env['mp'].setattr(S8, 'has_any_position', lambda sym: True)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:7]
    assert j.decision.reason == 'existing_position'


def test_s8_invalid_price(env):
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: None)
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:8]
    assert j.decision.reason == 'price_unavailable'


def test_s8_entry_mode_unconfirmed(env):
    """price>ema20 且 rsi<65 → UNCONFIRMED。"""
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '102.0'})
    env['mp'].setattr(S8, 'read_s3_market_data', lambda: {
        'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                     '15m': {'rsi': 50.0}}})
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:9]
    assert j.gates[-1].name == 'entry_mode' and j.gates[-1].passed is False
    assert j.decision.reason == 'entry_mode_unconfirmed'


def test_s8_extension_rejection(env):
    """追空过远：price 96 / ema20 101 / atr 1 → ext 5 > 2 → REJECT。"""
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '96.0'})
    env['mp'].setattr(S8, 'read_s3_market_data', lambda: {
        'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                     '15m': {'rsi': 45.0}}})
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER[:11]
    assert j.gates[-1].name == 'extension' and j.gates[-1].passed is False
    assert j.decision.reason == 'overextended'


def test_s8_atr_rejection(env):
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    env['mp'].setattr(S8, 'read_s3_market_data', lambda: {
        'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 7.0},
                     '15m': {'rsi': 45.0}}})
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert gate_names(j) == S8_GATE_ORDER
    assert j.gates[-1].name == 'atr' and j.gates[-1].passed is False
    assert j.decision.reason == 'atr_exceeded'


def test_s8_pump_down_uptrend_rejection(env):
    """PUMP_DOWN × 高周期上行 → REJECT pump_down_uptrend。"""
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    env['mp'].setattr(S8, 'read_s3_market_data', lambda: {
        'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                     '15m': {'rsi': 45.0},
                     '4h': {'ema20': 90.0, 'chg': 5.0},
                     '24h': {'chg': 20.0}}})
    run_s8(S8, EVENT_PUMP_DOWN)
    j = env['mem'].records[0]
    assert 'pump_guard' in gate_names(j)
    idx = gate_names(j).index('pump_guard')
    assert j.gates[idx].passed is False
    assert j.decision.reason == 'pump_down_uptrend'


def test_s8_pump_down_guard_passes_when_downtrend(env):
    """PUMP_DOWN × 价格低于 4h EMA20 → guard 不触发，正常继续。"""
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '85.0'})
    env['mp'].setattr(S8, 'read_s3_market_data', lambda: {
        'TESTUSDT': {'1h': {'ema20': 86.0, 'atr': 1.0, 'atr_pct': 2.0},
                     '15m': {'rsi': 45.0},
                     '4h': {'ema20': 90.0, 'chg': 5.0},
                     '24h': {'chg': 20.0}}})
    run_s8(S8, EVENT_PUMP_DOWN)
    j = env['mem'].records[0]
    pump = j.gates[gate_names(j).index('pump_guard')]
    assert pump.passed is True
    assert j.decision.action == 'OPEN'


def test_s8_confirmed_strength_59_rejected(env):
    """确认型短空 strength 59 < 60 → 第 1 关即拒。"""
    run_s8(S8, dict(EVENT_SHORT, strength=59))
    j = env['mem'].records[0]
    assert gate_names(j) == ['strength']
    assert j.gates[0].passed is False
    assert j.decision.reason == 'strength_below_minimum'


def test_s8_confirmed_strength_60_passes(env):
    """strength 60 → 通过（锁定 >= 边界）。"""
    run_s8(S8, dict(EVENT_SHORT, strength=60))
    j = env['mem'].records[0]
    assert j.gates[0].name == 'strength' and j.gates[0].passed is True
    assert j.decision.action == 'OPEN'


def test_s8_trend_gate_unreachable_for_right_momentum(env):
    """Observed Current Behavior：与 S6 镜像——classify SHORT 的
    RIGHT_MOMENTUM 要求 price<=ema20，trend gate 拒绝条件 price>ema20
    互斥 → 不可达。锁定 price == ema20 边界。
    """
    env['mp'].setattr(S8, 'fapi_get', lambda path, params=None: {'price': '101.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 45.0}}}
    run_s8(S8, EVENT_SHORT, market)
    j = env['mem'].records[0]
    trend_gate = j.gates[gate_names(j).index('trend')]
    assert trend_gate.passed is True
    assert j.decision.action == 'OPEN'


def test_s8_score_in_decision(env):
    """score = strength 70 - extension (1.0×4) = 66（探针冻结）。"""
    run_s8(S8, EVENT_SHORT)
    j = env['mem'].records[0]
    assert j.decision.final_score == 66
    assert j.strategy.score == 66
