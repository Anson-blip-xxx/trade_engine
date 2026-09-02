"""DecisionJournalBuilder 测试：只组装 / fail-safe / 字段过滤。"""

from journal import DecisionAction, validate_journal
from journal.builder import DecisionJournalBuilder, signal_source_from_event


def _build(**kw):
    jb = DecisionJournalBuilder(**kw)
    return jb.build(action='OPEN', accepted=True)


def test_full_build():
    """完整字段构造：signal/market/regime/strategy/gates/risk/metadata 全部落位。"""
    jb = DecisionJournalBuilder(
        signal_source='S3', signal_type='PULSE_UP', symbol='BTCUSDT', side='LONG',
        strength=78, event_id='evt-1', raw={'k': 1},
        strategy='S6', process='S6', pid=123, correlation_id='corr-1',
    )
    jb.set_regime(regime='weak_bull', risk_level='NORMAL', confidence=0.82,
                  source='S0', timestamp=1788000000)
    jb.set_market(price=100.0, bid=99.9, ask=100.1, ema20=99.0, atr=1.0,
                  atr_pct=2.0, rsi=55.0, taker_buy_ratio=0.6)
    jb.gate('regime', True, value='weak_bull')
    jb.gate('trend', True, value=100.0, threshold=99.0)
    jb.set_strategy_score(75)
    jb.set_entry_mode('RIGHT_MOMENTUM')
    jb.set_risk(position_size=0.5, leverage=3, stop_pct=0.04)

    j = jb.build(action=DecisionAction.OPEN.value, accepted=True, final_score=75)
    assert j is not None
    assert validate_journal(j) == []
    assert j.signal.source == 'S3' and j.signal.symbol == 'BTCUSDT'
    assert j.signal.raw == {'k': 1}
    assert j.regime.regime == 'weak_bull' and j.regime.source == 'S0'
    assert isinstance(j.regime.timestamp, str) and j.regime.timestamp.endswith('Z')
    assert j.market.price == 100.0 and j.market.ema20 == 99.0
    assert [g.name for g in j.gates] == ['regime', 'trend']
    assert [g.stage for g in j.gates] == [1, 2]
    assert j.strategy.score == 75 and j.strategy.entry_mode == 'RIGHT_MOMENTUM'
    assert j.risk.position_size == 0.5 and j.risk.leverage == 3
    assert j.decision.accepted is True
    assert j.metadata.process == 'S6' and j.metadata.pid == 123


def test_minimal_build():
    """最小构造：只给 signal 基本字段，其余默认，gates 为空。"""
    j = _build(signal_source='S3', signal_type='TREND_UP', symbol='BTCUSDT', side='LONG')
    assert j.signal.symbol == 'BTCUSDT'
    assert j.gates == ()
    assert j.risk is None
    assert j.market.price is None and j.market.bid is None
    assert validate_journal(j) == []


def test_source_mapping():
    """source 不受限：S3/TV/AI/MANUAL/REPLAY/未知 均可。"""
    for src in ('S3', 'TRADINGVIEW', 'AI', 'MANUAL', 'REPLAY', 'FUTURE_X'):
        j = _build(signal_source=src, signal_type='T', symbol='BTCUSDT', side='LONG')
        assert j.signal.source == src


def test_signal_source_from_event():
    """事件 payload → 来源标签映射。"""
    assert signal_source_from_event({}) == 'S3'                      # 无标记默认 S3
    assert signal_source_from_event({'source': 'tv'}) == 'TRADINGVIEW'
    assert signal_source_from_event({'source': 'S3'}) == 'S3'
    assert signal_source_from_event({'source': 'weird'}) == 'WEIRD'
    assert signal_source_from_event(None) == 'S3'


def test_gate_stage_auto_increment():
    """stage 按记录顺序自动 1..N。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG')
    jb.gate('a', True)
    jb.gate('b', False, reason='no')
    jb.gate('c', True)
    j = jb.build(action='REJECT', accepted=False, reason='no')
    assert [g.stage for g in j.gates] == [1, 2, 3]
    assert [g.passed for g in j.gates] == [True, False, True]


def test_early_rejection_shape():
    """早期拒绝：只含已执行 Gate，decision=REJECT，risk=None。"""
    jb = DecisionJournalBuilder(signal_source='S3', signal_type='TREND_UP',
                                symbol='BTCUSDT', side='LONG')
    jb.gate('regime', True, value='range')
    jb.gate('fresh', False, value=False, reason='stale')
    j = jb.build(action='REJECT', accepted=False, reason='event_stale')
    assert j.risk is None
    assert [g.name for g in j.gates] == ['regime', 'fresh']
    assert j.gates[-1].passed is False
    assert j.decision.action == 'REJECT'
    assert validate_journal(j) == []


def test_risk_none_vs_present():
    """未 set_risk → risk=None；set_risk 后字段完整。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG')
    j1 = jb.build(action='REJECT', accepted=False)
    assert j1.risk is None

    jb.set_risk(position_size=0.5, leverage=3, stop_pct=0.05)
    j2 = jb.build(action='OPEN', accepted=True)
    assert j2.risk is not None
    assert j2.risk.position_size == 0.5 and j2.risk.leverage == 3


def test_market_null_fields_and_extra_filtered():
    """未设置的 market 字段为 None；未知字段被过滤（不进模型）。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG')
    jb.set_market(price=1.0, totally_unknown_field=42)
    j = jb.build(action='OPEN', accepted=True)
    assert j.market.price == 1.0
    assert j.market.bid is None and j.market.open_interest is None
    assert not hasattr(j.market, 'totally_unknown_field')


def test_metadata_fields():
    """metadata 各字段落位；git_commit 由调用方提供。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG',
                                environment='PAPER', git_commit='abc123',
                                correlation_id='c-1', parent_signal_id='p-1')
    j = jb.build(action='OPEN', accepted=True)
    assert j.metadata.environment == 'PAPER'
    assert j.metadata.git_commit == 'abc123'
    assert j.metadata.correlation_id == 'c-1'


def test_builder_never_raises_on_garbage():
    """垃圾输入：任何方法都不抛异常（fail-safe）。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG',
                                strength='not-a-number')
    assert jb._signal_kwargs['strength'] is None  # 解析失败记 None，不猜

    # gate value 为不可序列化对象也不抛
    assert jb.gate('weird', 'truthy-string', value=object()) is True
    # set_risk 非法 leverage → risk 被丢弃而不是崩溃
    jb.set_risk(leverage='abc')
    # set_market 未知字段 → 过滤
    jb.set_market(nope=1)
    j = jb.build(action='OPEN', accepted=True)
    assert j is not None
    assert j.signal.strength is None
    assert j.risk is None


def test_build_returns_none_when_broken():
    """builder 内部损坏（_broken）→ build 返回 None、gate 不再累积、永不抛。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG')
    jb._broken = True  # 白盒：模拟内部故障
    assert jb.gate('a', True) is True          # 返回值不变
    assert jb.build(action='OPEN', accepted=True) is None


def test_epoch_timestamp_coercion():
    """epoch 秒自动转 UTC ISO-8601（signal/regime）。"""
    jb = DecisionJournalBuilder(signal_source='S3', symbol='X', side='LONG',
                                signal_timestamp=1788000000)
    jb.set_regime(regime='range', timestamp=1788000000)
    j = jb.build(action='OPEN', accepted=True)
    assert j.signal.signal_timestamp.endswith('Z')
    assert j.regime.timestamp.endswith('Z')
    assert validate_journal(j) == []
