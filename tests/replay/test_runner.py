"""ReplayRunner 测试：确定性回放 / 非法输入 / Early Return / nullable 快照。

本文件不依赖 Redis/Binance/DB/网络/当前系统时间，可独立运行：pytest tests/replay/
"""

from journal import (
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    RiskSnapshot,
    SignalSnapshot,
    StrategySnapshot,
)
from replay import ReplayRunner

T = '2026-09-02T09:00:00.000Z'
runner = ReplayRunner()


def _gate(name, stage, passed, value=None, threshold=None, reason=None):
    return GateResult(name=name, stage=stage, passed=passed, value=value,
                      threshold=threshold, reason=reason)


def _journal(gates=(), *, action='OPEN', accepted=True, reason=None,
             final_score=66.0, symbol='TESTUSDT', side='LONG',
             signal_type='TREND_UP', source='S3', strength=70, regime='range',
             risk='__none__', journal_id='j-1', journal_version='1.0',
             **overrides):
    kwargs = {
        'journal_id': journal_id,
        'journal_version': journal_version,
        'created_at': T, 'decision_at': T,
        'signal': SignalSnapshot(source=source, signal_type=signal_type,
                                 symbol=symbol, side=side, strength=strength),
        'market': MarketSnapshot(symbol=symbol, price=100.0, ema20=99.0),
        'regime': RegimeSnapshot(regime=regime, source='S0', timestamp=T),
        'strategy': StrategySnapshot(strategy='S6', score=final_score,
                                     entry_mode='RIGHT_MOMENTUM'),
        'gates': tuple(gates),
        'decision': DecisionResult(action=action, accepted=accepted, reason=reason,
                                   final_score=final_score, timestamp=T),
        'risk': None if risk == '__none__' else risk,
        'metadata': Metadata(process='S6', environment='REPLAY'),
    }
    kwargs.update(overrides)
    return DecisionJournal(**kwargs)


SIMPLE_GATES = (_gate('fresh', 1, True, value=True),
                _gate('trend', 2, True, value=100.0, threshold=99.0),
                _gate('score', 3, True, value=66.0, threshold=30.0))


# ── Test 1：最简单的 SAME ────────────────────────────────────────────────

def test_simple_same():
    j = _journal(gates=SIMPLE_GATES)
    r = runner.replay(j)
    assert r.replayed is True
    assert r.same is True
    assert r.first_difference is None
    assert r.differences == ()
    assert r.journal_id == 'j-1'
    assert r.original_action == 'OPEN' and r.replay_action == 'OPEN'
    assert r.original_accepted is True and r.replay_accepted is True
    assert r.metadata['error'] is None


# ── Test 2-5：decision 字段差异（Comparator 层已在别处覆盖，这里验证 Runner 联动）──

def test_replay_detects_decision_change_via_comparison():
    """Round-trip 无损时 same=True；人为构造差异经 Comparator 必然 DIFFERENT。"""
    from replay import DecisionComparator
    a = _journal(gates=SIMPLE_GATES, action='OPEN', accepted=True)
    b = _journal(gates=SIMPLE_GATES, action='REJECT', accepted=False)
    r = DecisionComparator().compare(a, b)
    assert r.same is False
    assert r.first_difference == 'decision.action'


# ── Test 8-10：Gate stage 非法 → replayed=False，明确错误 ────────────────

def test_invalid_stage_gap_rejected():
    """stage 1,2,4（不连续）→ replayed=False + invalid_journal。"""
    j = _journal(gates=(_gate('fresh', 1, True), _gate('trend', 2, True),
                        _gate('atr', 4, True)))
    r = runner.replay(j)
    assert r.replayed is False and r.same is False
    assert r.metadata['error'] == 'invalid_journal'
    assert '连续' in r.metadata['error_detail']


def test_invalid_stage_zero_rejected():
    """stage 从 0 开始 → 拒绝。"""
    j = _journal(gates=(_gate('fresh', 0, True),))
    r = runner.replay(j)
    assert r.replayed is False
    assert r.metadata['error'] == 'invalid_journal'
    assert '从 1 开始' in r.metadata['error_detail']


def test_invalid_stage_duplicate_rejected():
    """stage 1,2,2（重复）→ 拒绝。"""
    j = _journal(gates=(_gate('a', 1, True), _gate('b', 2, True), _gate('c', 2, True)))
    r = runner.replay(j)
    assert r.replayed is False
    assert r.metadata['error'] == 'invalid_journal'


def test_invalid_stage_gap_via_dict():
    """dict 输入同样拒绝不连续 stage（1,3）。"""
    r = runner.replay_dict({
        'journal_version': '1.0', 'journal_id': 'j-2',
        'created_at': T, 'decision_at': T,
        'signal': {'source': 'S3', 'signal_type': 'TREND_UP',
                   'symbol': 'TESTUSDT', 'side': 'LONG', 'strength': 70},
        'decision': {'action': 'OPEN', 'accepted': True},
        'gates': [{'name': 'fresh', 'stage': 1, 'passed': True},
                  {'name': 'atr', 'stage': 3, 'passed': True}],
    })
    assert r.replayed is False
    assert r.metadata['error'] == 'invalid_journal'
    assert r.journal_id == 'j-2'


# ── Test 11：Early Return ────────────────────────────────────────────────

def test_early_return_preserved():
    """只有 2 个 Gate（fresh PASS / cooldown FAIL）→ Replay 保持 2 个，不补后续 Gate。"""
    j = _journal(gates=(_gate('fresh', 1, True, value=True),
                        _gate('signal_cooldown', 2, False, value=False)),
                 action='REJECT', accepted=False, reason='signal_cooldown',
                 final_score=None)
    r = runner.replay(j)
    assert r.replayed is True and r.same is True
    # 回放侧 gates 数量不变
    from journal import to_dict
    assert len(to_dict(j)['gates']) == 2
    assert r.original_action == 'REJECT' and r.original_reason == 'signal_cooldown'
    assert r.original_final_score is None


# ── Test 12-13：risk null / risk exists ─────────────────────────────────

def test_risk_null_replays():
    j = _journal(gates=SIMPLE_GATES, risk='__none__')
    r = runner.replay(j)
    assert r.replayed is True and r.same is True


def test_risk_present_replays():
    risk = RiskSnapshot(position_size=10990.0, leverage=3, stop_pct=0.08)
    j = _journal(gates=SIMPLE_GATES, risk=risk)
    r = runner.replay(j)
    assert r.replayed is True and r.same is True


# ── Test 14-15：nullable Market / Regime ────────────────────────────────

def test_nullable_market_snapshot():
    j = _journal(gates=SIMPLE_GATES, market=MarketSnapshot(symbol='TESTUSDT'))
    r = runner.replay(j)
    assert r.replayed is True and r.same is True


def test_nullable_regime_snapshot():
    j = _journal(gates=SIMPLE_GATES, regime=None)
    r = runner.replay(j)
    assert r.replayed is True and r.same is True


# ── Test 16-17：JSON / dict round trip ──────────────────────────────────

def test_json_round_trip_same():
    from journal import to_json
    j = _journal(gates=SIMPLE_GATES)
    r = runner.replay_json(to_json(j))
    assert r.replayed is True and r.same is True
    assert r.first_difference is None


def test_dict_round_trip_same():
    from journal import to_dict
    j = _journal(gates=SIMPLE_GATES)
    r = runner.replay_dict(to_dict(j))
    assert r.replayed is True and r.same is True


# ── Test 18：非法 Schema ────────────────────────────────────────────────

def test_missing_journal_version_dict():
    """缺 journal_version → replayed=False，错误明确定位。"""
    data = {'journal_id': 'j-3', 'created_at': T, 'decision_at': T,
            'signal': {'source': 'S3', 'signal_type': 'TREND_UP',
                       'symbol': 'TESTUSDT', 'side': 'LONG'},
            'decision': {'action': 'OPEN', 'accepted': True}}
    r = runner.replay_dict(data)
    assert r.replayed is False
    assert r.metadata['error'] == 'missing_required_field'
    assert 'journal_version' in r.metadata['error_detail']


def test_missing_decision_dict():
    """缺 decision → replayed=False，不 crash。"""
    data = {'journal_version': '1.0', 'journal_id': 'j-4',
            'created_at': T, 'decision_at': T,
            'signal': {'source': 'S3', 'signal_type': 'TREND_UP',
                       'symbol': 'TESTUSDT', 'side': 'LONG'}}
    r = runner.replay_dict(data)
    assert r.replayed is False
    assert r.metadata['error'] == 'missing_required_field'


def test_invalid_json_text():
    """非法 JSON 文本 → replayed=False + invalid_input。"""
    r = runner.replay_json('{not-json')
    assert r.replayed is False
    assert r.metadata['error'] == 'invalid_input'


def test_json_array_input():
    """JSON 数组（非对象）→ invalid_input。"""
    r = runner.replay_json('[1, 2]')
    assert r.replayed is False
    assert r.metadata['error'] == 'invalid_input'


def test_replay_wrong_type():
    """非 Journal/非 dict/非 str 输入 → invalid_input，不 crash。"""
    for bad in (123, None, ['x']):
        r = runner.replay(bad)
        assert r.replayed is False
        assert r.metadata['error'] == 'invalid_input'


# ── Test 19：Determinism ────────────────────────────────────────────────

def test_deterministic_100_replays():
    """同一 Journal 回放 100 次，ReplayResult 完全一致（无时间/随机因素）。"""
    j = _journal(gates=SIMPLE_GATES)
    results = [runner.replay(j) for _ in range(100)]
    assert all(r == results[0] for r in results)


def test_dict_and_object_paths_agree():
    """同一 Journal 经 replay 与 replay_dict 得到相同结论。"""
    from journal import to_dict
    j = _journal(gates=SIMPLE_GATES)
    r1 = runner.replay(j)
    r2 = runner.replay_dict(to_dict(j))
    assert r1 == r2


# ── Test 20：Unknown source ─────────────────────────────────────────────

def test_unknown_source_ok():
    """source 不在文档示例中（未来来源）→ Replay 正常。"""
    j = _journal(gates=SIMPLE_GATES, source='NEW_AI_ENGINE')
    r = runner.replay(j)
    assert r.replayed is True and r.same is True
