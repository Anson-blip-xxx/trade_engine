"""P1-02 集成回归：S6/S8 接入 Journal 后决策行为完全一致。

方法论：
- 对同一场景分别以 Null（等价于无 Journal）与 Memory / Broken Recorder 运行
  S6._open_long / S8._open_short，断言 open_position 收到的参数、返回值完全一致。
- 用硬编码的期望值（score/leverage/stop_price）锁定行为基线——这些值在
  接入 Journal 前后必须相同（Journal 不参与任何计算）。
- Journal 内容断言：gate 顺序、early-reject 形态、risk=None 语义。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STRATEGIES = _ROOT / 'strategies'
for _p in (str(_ROOT), str(_STRATEGIES)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import S6
import S8

from journal import validate_journal
from journal.recorder import MemoryJournalRecorder, NullJournalRecorder


class BrokenRecorder:
    def record(self, journal):
        raise RuntimeError('journal boom')


EVT_LONG = {'symbol': 'TESTUSDT', 'type': 'TREND_UP', 'strength': 70}
EVT_SHORT = {'symbol': 'TESTUSDT', 'type': 'TREND_DOWN', 'strength': 70}
MARKET_LONG = {'TESTUSDT': {'1h': {'ema20': 99.0, 'atr': 1.0, 'atr_pct': 2.0},
                            '15m': {'rsi': 55.0},
                            '4h': {'chg': 1.0}, '24h': {'chg': 5.0}}}
MARKET_SHORT = {'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                             '15m': {'rsi': 45.0},
                             '4h': {'chg': -1.0}, '24h': {'chg': -5.0}}}
STATE = {'cooldowns': {}}

S6_GATE_ORDER = ['regime', 'fresh', 'signal_cooldown', 'position_limit',
                 'symbol_cooldown', 'market_allowed', 'existing_position',
                 'price', 'entry_mode', 'trend', 'extension', 'atr']
S8_GATE_ORDER = ['strength', 'fresh', 'signal_cooldown', 'position_limit',
                 'symbol_cooldown', 'market_allowed', 'existing_position',
                 'price', 'entry_mode', 'trend', 'extension', 'atr']


def _patch_common(mp, mod, *, open_result=True, market_allows=True, has_pos=False,
                  stale=False, sig_fresh=True, dd='normal', pos_count=0,
                  price='100.0', calc_qty=0.5, market=None):
    """只替换 IO/边界依赖；其余（score/trend/extension 判定）用真实函数。"""
    calls = {'open': []}

    def _fake_open(*args, **kwargs):
        calls['open'].append({'args': args, 'kwargs': kwargs})
        return open_result

    mp.setattr(mod, 'get_market_state', lambda: {'regime': 'range', 'timestamp': 1788000000},
               raising=False)  # S8 未导入该符号（其 market_allows_trading 内部读 S0）
    if market is not None:
        # S8._open_short 内部会重新拉取行情快照，必须替换为测试数据
        mp.setattr(mod, 'read_s3_market_data', lambda: market, raising=False)
    mp.setattr(mod, 'event_is_stale', lambda evt: stale)
    mp.setattr(mod, 'is_event_fresh', lambda *a, **k: sig_fresh)
    mp.setattr(mod, 'drawdown_mode', lambda: dd)
    mp.setattr(mod, 'maybe_replace_recovery_position', lambda *a, **k: True)
    mp.setattr(mod, 'get_position_count', lambda name: pos_count)
    mp.setattr(mod, 'market_allows_trading', lambda name, side: market_allows)
    mp.setattr(mod, 'has_any_position', lambda sym: has_pos)
    mp.setattr(mod, 'fapi_get', lambda path, params=None: {'price': price})
    mp.setattr(mod, 'get_short_ratio', lambda sym: 0.5)
    mp.setattr(mod, 'calc_position_qty', lambda *a, **k: calc_qty)
    mp.setattr(mod, 'open_position', _fake_open)
    mp.setattr(mod, 'release_event_fresh', lambda *a, **k: None)
    return calls


def _patch_recorder(mp, mod, recorder):
    mp.setattr(mod, 'get_default_recorder', lambda: recorder)


# ═══════════════════ S6 ═══════════════════

def test_s6_accepted_decision_baseline(monkeypatch):
    """正常信号：Journal 接入前后 open_position 参数逐字段一致（行为基线）。"""
    calls_null = _patch_common(monkeypatch, S6)
    _patch_recorder(monkeypatch, S6, NullJournalRecorder())
    ret_null = S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    # 旧行为基线（与 P1-02 之前一致）：这些值由未改动的计算链产出
    args = calls_null['open'][0]['args']
    assert args[0] == 'S6A' and args[1] == 'TESTUSDT' and args[2] == 'LONG'
    assert args[3] == 100.0
    assert args[4] == pytest.approx(92.0)          # stop = 100*(1-0.08)
    assert args[5] == 0.5                          # qty（monkeypatch）
    assert args[7] == 3                            # leverage_for_score(TREND_UP, 66, 2.0)
    assert args[8] == 'TREND_UP'
    assert calls_null['open'][0]['kwargs']['decision_context']['strength'] == 66
    assert ret_null is not None

    # Memory recorder 运行：open_position 参数与 Null 运行完全一致
    mem = MemoryJournalRecorder()
    calls_mem = _patch_common(monkeypatch, S6)
    _patch_recorder(monkeypatch, S6, mem)
    S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)
    assert calls_mem['open'] == calls_null['open']

    # Journal 内容：gate 顺序 / decision / risk
    assert len(mem) == 1
    j = mem.records[0]
    assert validate_journal(j) == []
    assert [g.name for g in j.gates] == S6_GATE_ORDER
    assert [g.stage for g in j.gates] == list(range(1, len(S6_GATE_ORDER) + 1))
    assert all(g.passed for g in j.gates)
    assert j.decision.action == 'OPEN' and j.decision.accepted is True
    assert j.decision.final_score == 66
    assert j.signal.source == 'S3' and j.signal.side == 'LONG'
    assert j.signal.raw == EVT_LONG
    assert j.regime.regime == 'range' and j.regime.source == 'S0'
    assert j.market.price == 100.0 and j.market.ema20 == 99.0
    assert j.strategy.strategy == 'S6'
    assert j.strategy.score == 66 and j.strategy.entry_mode == 'RIGHT_MOMENTUM'
    assert j.risk.position_size == 0.5 and j.risk.leverage == 3
    assert j.metadata.process == 'S6'


def test_s6_early_reject_journal(monkeypatch):
    """Gate 早期失败：open 不被调用，Journal 只含已执行 Gate，decision=REJECT。"""
    calls = _patch_common(monkeypatch, S6, market_allows=False)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S6, mem)
    S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    assert calls['open'] == []                       # 未执行到开仓
    assert len(mem) == 1
    j = mem.records[0]
    assert [g.name for g in j.gates] == S6_GATE_ORDER[:6]   # 到 market_allowed 为止
    assert j.gates[-1].name == 'market_allowed' and j.gates[-1].passed is False
    assert j.decision.action == 'REJECT'
    assert j.decision.accepted is False
    assert j.decision.reason == 'market_disallowed'
    assert j.risk is None                            # Risk 之前被拒 → None
    assert j.decision.final_score is None            # score 尚未计算 → None
    assert validate_journal(j) == []


def test_s6_open_rejected_records_risk(monkeypatch):
    """open_position 拒绝（如 score/分析过滤在执行层拒绝）→ decision 记录真实结果。"""
    calls = _patch_common(monkeypatch, S6, open_result=False)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S6, mem)
    S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    assert len(calls['open']) == 1
    assert len(mem) == 1
    j = mem.records[0]
    assert j.decision.action == 'OPEN'
    assert j.decision.accepted is False
    assert j.decision.reason == 'open_position_rejected'
    assert j.risk is not None                        # Risk 已计算 → 记录，不伪造
    assert j.risk.position_size == 0.5


def test_s6_risk_reject_qty_zero(monkeypatch):
    """Risk 层产出 0 仓位（risk reject）→ 决策不变，Journal 记录真实 qty。"""
    calls = _patch_common(monkeypatch, S6, calc_qty=0.0, open_result=False)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S6, mem)
    S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    assert calls['open'][0]['args'][5] == 0.0
    j = mem.records[0]
    assert j.decision.accepted is False
    assert j.risk.position_size == 0.0


def test_s6_fail_open_broken_recorder(monkeypatch):
    """Recorder 崩溃：S6 决策完全不变（open 参数一致、无异常外抛）。"""
    calls_null = _patch_common(monkeypatch, S6)
    _patch_recorder(monkeypatch, S6, NullJournalRecorder())
    S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    calls_broken = _patch_common(monkeypatch, S6)
    _patch_recorder(monkeypatch, S6, BrokenRecorder())
    ret = S6._open_long(dict(STATE), dict(EVT_LONG), MARKET_LONG)

    assert ret is not None
    assert calls_broken['open'] == calls_null['open']   # 决策逐字节一致


def test_s6_source_mapping_tradingview(monkeypatch):
    """TV 来源事件：signal.source=TRADINGVIEW，决策不变。"""
    calls = _patch_common(monkeypatch, S6)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S6, mem)
    evt = dict(EVT_LONG, source='tv')
    S6._open_long(dict(STATE), evt, MARKET_LONG)
    assert mem.records[0].signal.source == 'TRADINGVIEW'
    assert calls['open'][0]['kwargs']['decision_context']['strength'] == 66


# ═══════════════════ S8 ═══════════════════

def test_s8_accepted_decision_baseline(monkeypatch):
    """S8 正常信号：行为基线 + Journal 内容。"""
    calls_null = _patch_common(monkeypatch, S8, market=MARKET_SHORT)
    _patch_recorder(monkeypatch, S8, NullJournalRecorder())
    S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)
    args = calls_null['open'][0]['args']
    assert args[0] == 'S8' and args[1] == 'TESTUSDT' and args[2] == 'SHORT'
    assert args[3] == 100.0
    assert args[4] == pytest.approx(108.0)         # stop = 100*(1+0.08)
    assert args[7] == 3                            # leverage_for_score(TREND_DOWN, 66, 2.0)
    assert calls_null['open'][0]['kwargs']['decision_context']['strength'] == 66

    mem = MemoryJournalRecorder()
    calls_mem = _patch_common(monkeypatch, S8, market=MARKET_SHORT)
    _patch_recorder(monkeypatch, S8, mem)
    S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)
    assert calls_mem['open'] == calls_null['open']

    j = mem.records[0]
    assert validate_journal(j) == []
    assert [g.name for g in j.gates] == S8_GATE_ORDER
    assert all(g.passed for g in j.gates)
    assert j.decision.action == 'OPEN' and j.decision.accepted is True
    assert j.signal.side == 'SHORT' and j.signal.source == 'S3'
    assert j.strategy.strategy == 'S8' and j.strategy.score == 66
    assert j.regime.regime is None                 # S8 决策层未直接持有 S0 快照 → 不伪造
    assert j.risk.position_size == 0.5


def test_s8_strength_gate_reject(monkeypatch):
    """S8 短空强度门槛（raw_strength<60）→ 早期拒绝 Journal。"""
    calls = _patch_common(monkeypatch, S8)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S8, mem)
    S8._open_short(dict(STATE), dict(EVT_SHORT, strength=36), MARKET_SHORT)

    assert calls['open'] == []
    j = mem.records[0]
    assert [g.name for g in j.gates] == ['strength']
    assert j.gates[0].passed is False
    assert j.gates[0].value == 36.0
    assert j.decision.action == 'REJECT'
    assert j.decision.reason == 'strength_below_minimum'
    assert j.risk is None


def test_s8_early_reject_journal(monkeypatch):
    """S8 早期失败（已有持仓）：只含已执行 Gate。"""
    calls = _patch_common(monkeypatch, S8, has_pos=True, market=MARKET_SHORT)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S8, mem)
    S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)

    assert calls['open'] == []
    j = mem.records[0]
    assert [g.name for g in j.gates] == S8_GATE_ORDER[:7]
    assert j.gates[-1].name == 'existing_position' and j.gates[-1].passed is False
    assert j.decision.action == 'REJECT'
    assert j.decision.reason == 'existing_position'
    assert j.risk is None


def test_s8_open_rejected_records_risk(monkeypatch):
    """S8 open_position 拒绝 → decision 记录真实结果，Risk 已计算则记录。"""
    calls = _patch_common(monkeypatch, S8, open_result=False, market=MARKET_SHORT)
    mem = MemoryJournalRecorder()
    _patch_recorder(monkeypatch, S8, mem)
    S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)

    assert len(calls['open']) == 1
    j = mem.records[0]
    assert j.decision.action == 'OPEN' and j.decision.accepted is False
    assert j.decision.reason == 'open_position_rejected'
    assert j.risk is not None


def test_s8_fail_open_broken_recorder(monkeypatch):
    """S8 Recorder 崩溃：决策完全不变。"""
    calls_null = _patch_common(monkeypatch, S8, market=MARKET_SHORT)
    _patch_recorder(monkeypatch, S8, NullJournalRecorder())
    S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)

    calls_broken = _patch_common(monkeypatch, S8, market=MARKET_SHORT)
    _patch_recorder(monkeypatch, S8, BrokenRecorder())
    ret = S8._open_short(dict(STATE), dict(EVT_SHORT), MARKET_SHORT)

    assert ret is not None
    assert len(calls_null['open']) == 1
    assert calls_broken['open'] == calls_null['open']


# ═══════════════════ 约束检查 ═══════════════════

def test_s6_s8_do_not_use_serializer_or_conditional_journal():
    """S6/S8 源码约束：不直接用 serializer/to_json；Journal 不参与任何 if 判断。"""
    for path in (_STRATEGIES / 'S6.py', _STRATEGIES / 'S8.py'):
        src = path.read_text(encoding='utf-8')
        assert 'serializer' not in src, f'{path.name} 不应直接依赖 serializer'
        assert 'to_json' not in src and 'from_dict' not in src
        assert 'if jb' not in src and 'if journal' not in src
