"""DecisionComparator 测试：差异定位 / first_difference / 相等语义。"""
from journal import (
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    SignalSnapshot,
    StrategySnapshot,
)
from replay import DecisionComparator

T = '2026-09-02T09:00:00.000Z'
comparator = DecisionComparator()


def _gate(name, stage, passed, value=None, threshold=None, reason=None):
    return GateResult(name=name, stage=stage, passed=passed, value=value,
                      threshold=threshold, reason=reason)


def _journal(gates=(), *, action='OPEN', accepted=True, reason=None,
             final_score=66.0, symbol='TESTUSDT', side='LONG',
             signal_type='TREND_UP', strength=70, regime='range',
             risk='__none__', journal_id='j-1', process='S6'):
    kwargs = {
        'journal_id': journal_id,
        'created_at': T, 'decision_at': T,
        'signal': SignalSnapshot(source='S3', signal_type=signal_type,
                                 symbol=symbol, side=side, strength=strength),
        'market': MarketSnapshot(symbol=symbol, price=100.0, ema20=99.0),
        'regime': RegimeSnapshot(regime=regime, source='S0', timestamp=T),
        'strategy': StrategySnapshot(strategy='S6', score=final_score,
                                     entry_mode='RIGHT_MOMENTUM'),
        'gates': tuple(gates),
        'decision': DecisionResult(action=action, accepted=accepted, reason=reason,
                                   final_score=final_score, timestamp=T),
        'risk': None if risk == '__none__' else risk,
        'metadata': Metadata(process=process, environment='REPLAY'),
    }
    return DecisionJournal(**kwargs)


def test_identical_journals_same():
    """完全一致 → same=True，无差异。"""
    a = _journal(gates=(_gate('fresh', 1, True), _gate('trend', 2, True)))
    b = _journal(gates=(_gate('fresh', 1, True), _gate('trend', 2, True)))
    r = comparator.compare(a, b)
    assert r.same is True
    assert r.differences == ()
    assert r.first_difference is None


def test_action_mismatch():
    """action 不一致 → 定位 decision.action。"""
    a = _journal(action='OPEN', accepted=True)
    b = _journal(action='REJECT', accepted=False)
    r = comparator.compare(a, b)
    assert r.same is False
    assert r.first_difference == 'decision.action'
    assert r.differences[0].expected == 'OPEN'
    assert r.differences[0].actual == 'REJECT'


def test_accepted_mismatch():
    """accepted 不一致 → 定位 decision.accepted。"""
    a = _journal(action='OPEN', accepted=True)
    b = _journal(action='OPEN', accepted=False)
    r = comparator.compare(a, b)
    paths = [d.path for d in r.differences]
    assert 'decision.accepted' in paths
    assert r.first_difference == 'decision.accepted'


def test_reason_mismatch():
    """reason 不一致（None vs 字符串）→ 定位 decision.reason。"""
    a = _journal(action='REJECT', accepted=False, reason=None)
    b = _journal(action='REJECT', accepted=False, reason='trend_filter')
    r = comparator.compare(a, b)
    assert r.first_difference == 'decision.reason'
    assert r.differences[0].expected is None
    assert r.differences[0].actual == 'trend_filter'


def test_final_score_mismatch():
    """final_score 不一致 → 定位 decision.final_score。"""
    a = _journal(final_score=66.0)
    b = _journal(final_score=65.0)
    r = comparator.compare(a, b)
    assert r.first_difference == 'decision.final_score'
    assert r.differences[0].expected == 66.0
    assert r.differences[0].actual == 65.0


def test_gate_passed_mismatch():
    """第 3 个 Gate passed 不一致 → 定位 gates[2].passed。"""
    g = (_gate('fresh', 1, True), _gate('cooldown', 2, True), _gate('trend', 3, True))
    g2 = (_gate('fresh', 1, True), _gate('cooldown', 2, True), _gate('trend', 3, False))
    r = comparator.compare(_journal(gates=g), _journal(gates=g2))
    assert r.same is False
    assert r.first_difference == 'gates[2].passed'
    assert r.differences[0].expected is True
    assert r.differences[0].actual is False


def test_gate_order_change():
    """Gate 顺序变化 → 定位 gates[1].name。"""
    a = _journal(gates=(_gate('fresh', 1, True), _gate('cooldown', 2, True),
                        _gate('trend', 3, True)))
    b = _journal(gates=(_gate('fresh', 1, True), _gate('trend', 2, True),
                        _gate('cooldown', 3, True)))
    r = comparator.compare(a, b)
    assert r.same is False
    assert r.first_difference == 'gates[1].name'
    assert r.differences[0].expected == 'cooldown'
    assert r.differences[0].actual == 'trend'


def test_gate_count_mismatch_early_return_shape():
    """Gate 数量变化（early-return 形状改变）→ 定位 gates.count，不伪造缺的 Gate。"""
    a = _journal(gates=(_gate('fresh', 1, True), _gate('cooldown', 2, True),
                        _gate('trend', 3, True)))
    b = _journal(gates=(_gate('fresh', 1, True), _gate('cooldown', 2, False)))
    r = comparator.compare(a, b)
    assert r.same is False
    paths = [d.path for d in r.differences]
    assert paths[0] == 'gates[1].passed'      # 共同前缀内先定位
    assert 'gates.count' in paths             # 数量差异也被记录
    assert r.differences[0].expected is True


def test_gate_value_mismatch():
    """Gate value 不一致 → 定位 gates[i].value。"""
    a = _journal(gates=(_gate('atr', 1, True, value=2.0, threshold=6.0),))
    b = _journal(gates=(_gate('atr', 1, True, value=7.0, threshold=6.0),))
    r = comparator.compare(a, b)
    assert r.first_difference == 'gates[0].value'
    assert r.differences[0].expected == 2.0
    assert r.differences[0].actual == 7.0


def test_gate_threshold_mismatch():
    """Gate threshold 不一致 → 定位 gates[i].threshold。"""
    a = _journal(gates=(_gate('atr', 1, True, value=2.0, threshold=6.0),))
    b = _journal(gates=(_gate('atr', 1, True, value=2.0, threshold=5.0),))
    r = comparator.compare(a, b)
    assert r.first_difference == 'gates[0].threshold'


def test_multiple_differences_gates_before_decision():
    """gates 与 decision 同时差异：gates 先报（因果链在前），收集全部差异。"""
    a = _journal(gates=(_gate('fresh', 1, True),), action='OPEN', accepted=True)
    b = _journal(gates=(_gate('fresh', 1, False),), action='REJECT', accepted=False)
    r = comparator.compare(a, b)
    assert r.same is False
    assert r.first_difference == 'gates[0].passed'
    paths = [d.path for d in r.differences]
    assert paths == ['gates[0].passed', 'decision.action', 'decision.accepted']


def test_runtime_metadata_not_compared():
    """运行环境字段（journal_id/created_at/pid）不参与决策比较。"""
    a = _journal(journal_id='aaa', process='S6')
    b = _journal(journal_id='bbb', process='S6-other-run')
    r = comparator.compare(a, b)
    assert r.same is True


def test_bool_vs_int_value_not_equal():
    """bool 与数字不混同：True != 1。"""
    a = _journal(gates=(_gate('fresh', 1, True, value=True),))
    b = _journal(gates=(_gate('fresh', 1, True, value=1),))
    r = comparator.compare(a, b)
    assert r.same is False
    assert r.first_difference == 'gates[0].value'


def test_tuple_vs_list_normalized():
    """tuple 与 list 归一（序列化约定），视为相等。"""
    a = _journal(gates=(_gate('trend', 1, True, value=('a', 'b')),))
    b = _journal(gates=(_gate('trend', 1, True, value=['a', 'b']),))
    assert comparator.compare(a, b).same is True


def test_none_vs_false_not_equal():
    """None 与 False 语义不同（无阈值 vs 阈值为假）。"""
    a = _journal(gates=(_gate('trend', 1, True, threshold=None),))
    b = _journal(gates=(_gate('trend', 1, True, threshold=False),))
    assert comparator.compare(a, b).same is False
