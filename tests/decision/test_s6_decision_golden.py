"""Golden：S6 LONG Decision 全路径 characterization。

通过 Journal 记录器断言决策结果（action/reason/gates/score/entry_mode）。
Decision 业务函数全部真实执行；仅 IO / 进程状态被替换。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "strategies")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
import S6
from decision_helpers import (
    EVENT_LONG,
    EVENT_PUMP_UP,
    EVENT_VIOLANT_BULL,
    MARKET_TAKEOVER,
    S6_GATE_ORDER,
    gate_names,
    patch_s6,
    run_s6,
)

from journal import validate_journal
from journal.recorder import MemoryJournalRecorder


@pytest.fixture
def env(monkeypatch):
    mem = MemoryJournalRecorder()
    calls = patch_s6(monkeypatch, S6, recorder=mem)
    return {'calls': calls, 'mem': mem, 'mp': monkeypatch}


def test_s6_normal_path_full_freeze(env):
    """正常路径：12 Gate 全过 → OPEN，score=66，entry_mode=RIGHT_MOMENTUM。"""
    run_s6(S6, EVENT_LONG)
    assert len(env['calls']['open']) == 1
    j = env['mem'].records[0]
    assert validate_journal(j) == []
    assert gate_names(j) == S6_GATE_ORDER
    assert all(g.passed for g in j.gates)
    assert j.decision.action == 'OPEN' and j.decision.accepted is True
    assert j.decision.final_score == 66
    assert j.strategy.entry_mode == 'RIGHT_MOMENTUM'
    assert j.signal.side == 'LONG'
    args = env['calls']['open'][0]['args']
    assert args[0] == 'S6A' and args[7] == 3             # leverage_for_score(66, 2.0)


def test_s6_stale_event(env):
    env['mp'].setattr(S6, 'event_is_stale', lambda evt: True)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == ['regime', 'fresh']
    assert j.gates[-1].passed is False
    assert j.decision.action == 'REJECT'
    assert j.decision.reason == 'event_stale'
    assert j.decision.final_score is None


def test_s6_signal_cooldown(env):
    env['mp'].setattr(S6, 'is_event_fresh', lambda *a, **k: False)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == ['regime', 'fresh', 'signal_cooldown']
    assert j.decision.reason == 'signal_cooldown'


def test_s6_position_limit(env):
    env['mp'].setattr(S6, 'get_position_count', lambda name: 2)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:4]
    assert j.gates[-1].name == 'position_limit' and j.gates[-1].passed is False
    assert j.decision.reason == 'position_limit'


def test_s6_symbol_cooldown(env):
    run_s6(S6, EVENT_LONG, state={'cooldowns': {'TESTUSDT': 9999999999}})
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:5]
    assert j.decision.reason == 'symbol_cooldown'


def test_s6_market_risk_off(env):
    env['mp'].setattr(S6, 'market_allows_trading', lambda name, side: False)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:6]
    assert j.decision.reason == 'market_disallowed'


def test_s6_existing_position(env):
    env['mp'].setattr(S6, 'has_any_position', lambda sym: True)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:7]
    assert j.decision.reason == 'existing_position'


def test_s6_invalid_price(env):
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: None)
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:8]
    assert j.gates[-1].name == 'price' and j.gates[-1].passed is False
    assert j.decision.reason == 'price_unavailable'
    assert len(env['calls']['release']) == 1


def test_s6_entry_mode_unconfirmed(env):
    """price<ema20 且 rsi>35 → UNCONFIRMED → REJECT。"""
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '98.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 100.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 50.0}}}
    run_s6(S6, EVENT_LONG, market)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:9]
    assert j.gates[-1].name == 'entry_mode' and j.gates[-1].passed is False
    assert j.gates[-1].value == 'UNCONFIRMED'
    assert j.decision.reason == 'entry_mode_unconfirmed'


def test_s6_extension_rejection(env):
    """追多过远：price 104 / ema20 100 / atr 1 → ext 4 > 2 → REJECT。"""
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '104.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 100.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 60.0}}}
    run_s6(S6, EVENT_LONG, market)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER[:11]
    assert j.gates[-1].name == 'extension' and j.gates[-1].passed is False
    assert j.decision.reason == 'overextended'


def test_s6_atr_rejection(env):
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 99.0, 'atr': 1.0, 'atr_pct': 7.0},
                           '15m': {'rsi': 55.0}}}
    run_s6(S6, EVENT_LONG, market)
    j = env['mem'].records[0]
    assert gate_names(j) == S6_GATE_ORDER
    assert j.gates[-1].name == 'atr' and j.gates[-1].passed is False
    assert j.decision.reason == 'atr_exceeded'


def test_s6_violent_bullish_regime_restriction(env):
    """VIOLENT_BULLISH × weak_bear → 第 1 关即拒。"""
    env['mp'].setattr(S6, 'get_market_state',
                      lambda: {'regime': 'weak_bear', 'timestamp': 1788000000})
    run_s6(S6, EVENT_VIOLANT_BULL)
    j = env['mem'].records[0]
    assert gate_names(j) == ['regime']
    assert j.gates[0].passed is False
    assert j.decision.reason == 'regime_conflict'


def test_s6_takeover_ready_path(env):
    """S6B 趋势接管：system=S6B，leverage=2，entry_mode=S6B_TREND_TAKEOVER。"""
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    run_s6(S6, EVENT_PUMP_UP, MARKET_TAKEOVER)
    j = env['mem'].records[0]
    assert 'takeover' in gate_names(j)
    assert j.gates[gate_names(j).index('takeover')].passed is True
    assert j.decision.action == 'OPEN' and j.decision.accepted is True
    assert j.strategy.entry_mode == 'S6B_TREND_TAKEOVER'
    args = env['calls']['open'][0]['args']
    assert args[0] == 'S6B' and args[7] == 2            # takeover 固定杠杆 2
    assert args[4] == pytest.approx(90.0)               # stop 100*(1-0.10)


def test_s6_takeover_not_ready(env):
    """takeover 条件未满足（flow<0.52）→ REJECT takeover_not_ready。"""
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 99.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 60.0, 'ema20': 99.5, 'atr': 0.5,
                                   'taker_buy_ratio': 0.40},
                           '4h': {'ema20': 98.0, 'chg': 5.0},
                           '24h': {'ema20': 95.0, 'ema60': 90.0, 'chg': 15.0}}}
    run_s6(S6, EVENT_PUMP_UP, market)
    j = env['mem'].records[0]
    assert gate_names(j)[-1] == 'takeover'
    assert j.gates[-1].passed is False
    assert j.decision.reason == 'takeover_not_ready'


def test_s6_left_reversal_path(env):
    """LEFT_REVERSAL：price<ema20 + rsi<=35 → 走左侧，trend gate 通过。"""
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '98.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 100.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 30.0}}}
    run_s6(S6, EVENT_LONG, market)
    j = env['mem'].records[0]
    assert j.decision.action == 'OPEN'
    assert j.strategy.entry_mode == 'LEFT_REVERSAL'
    trend_gate = j.gates[gate_names(j).index('trend')]
    assert trend_gate.passed is True


def test_s6_trend_gate_unreachable_for_right_momentum(env):
    """Observed Current Behavior：classify LONG 的 RIGHT_MOMENTUM 要求
    price>=ema20，trend gate 拒绝条件是 price<ema20——互斥，因此该 gate
    在 classify 路径下不可达。锁定 price==ema20 边界 → 通过。
    Potential concern / Future phase: Phase 3-02 显式决策。
    """
    env['mp'].setattr(S6, 'fapi_get', lambda path, params=None: {'price': '100.0'})
    market = {'TESTUSDT': {'1h': {'ema20': 100.0, 'atr': 1.0, 'atr_pct': 2.0},
                           '15m': {'rsi': 55.0}}}
    run_s6(S6, EVENT_LONG, market)
    j = env['mem'].records[0]
    trend_gate = j.gates[gate_names(j).index('trend')]
    assert trend_gate.passed is True
    assert j.decision.action == 'OPEN'


def test_s6_score_in_decision(env):
    """score = strength 70 - extension (1.0×4) = 66（探针冻结）。"""
    run_s6(S6, EVENT_LONG)
    j = env['mem'].records[0]
    assert j.decision.final_score == 66
    assert j.strategy.score == 66
