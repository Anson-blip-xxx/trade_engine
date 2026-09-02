"""Unified Signal 模型测试：构造 / 归一 / immutable / 校验。"""
import dataclasses

import pytest

from signals import Signal, SignalSide, SignalValidationError, validate_signal


def test_minimal_signal():
    """最小合法 Signal：4 个必须字段，其余默认。"""
    s = Signal(symbol='BTCUSDT', side='LONG', signal_type='PULSE_UP',
               source='S3', strategy='S6')
    assert s.symbol == 'BTCUSDT'
    assert s.side == 'LONG'
    assert s.signal_type == 'PULSE_UP'
    assert s.source == 'S3'
    assert s.strategy == 'S6'
    assert s.strength is None
    assert s.timestamp is None
    assert s.event_id is None
    assert s.metadata == {}
    assert validate_signal(s) == []


def test_full_signal():
    """完整 Signal：全部字段落位。"""
    s = Signal(symbol='BTCUSDT', side='SHORT', signal_type='TREND_DOWN',
               source='TRADINGVIEW', strategy='S8', strength=72.0,
               timestamp='2026-09-02T09:00:00.000Z', event_id='evt-1',
               metadata={'tv_signal': 'TREND_DOWN_SHORT'})
    assert s.strength == 72.0
    assert s.timestamp == '2026-09-02T09:00:00.000Z'
    assert s.event_id == 'evt-1'
    assert s.metadata == {'tv_signal': 'TREND_DOWN_SHORT'}
    assert validate_signal(s) == []


def test_all_sides_construct():
    """四种 side 均可构造。"""
    for side in ('LONG', 'SHORT', 'NEUTRAL', 'UNKNOWN'):
        s = Signal(symbol='X', side=side, signal_type='T', source='S3',
                   strategy='S6')
        assert s.side == side


def test_immutable():
    """frozen：创建后不可修改。"""
    s = Signal(symbol='BTCUSDT', side='LONG', signal_type='TREND_UP',
               source='S3', strategy='S6')
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.symbol = 'ETHUSDT'
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.strength = 1.0


def test_equality_and_deterministic_repr():
    """相同值 → 相等且 repr 确定。"""
    a = Signal(symbol='BTCUSDT', side='LONG', signal_type='TREND_UP',
               source='S3', strategy='S6', strength=70)
    b = Signal(symbol='BTCUSDT', side='LONG', signal_type='TREND_UP',
               source='S3', strategy='S6', strength=70.0)
    assert a == b
    assert repr(a) == repr(b)


def test_metadata_defensive_copy():
    """metadata 构造时防御性拷贝：外部修改不影响 Signal。"""
    meta = {'raw': {'k': 1}}
    s = Signal(symbol='BTCUSDT', side='LONG', signal_type='TREND_UP',
               source='S3', strategy='S6', metadata=meta)
    meta['raw']['k'] = 999
    assert s.metadata['raw']['k'] == 1


def test_normalization_in_construction():
    """构造时自动归一：symbol/side/type/source。"""
    s = Signal(symbol=' btcusdt ', side=' buy ', signal_type=' trend_up ',
               source=' tv ', strategy=' s6 ')
    assert s.symbol == 'BTCUSDT'
    assert s.side == 'LONG'
    assert s.signal_type == 'TREND_UP'
    assert s.source == 'TRADINGVIEW'
    assert s.strategy == 'S6'


def test_epoch_timestamp_coercion():
    """epoch 秒 → UTC ISO-8601。"""
    s = Signal(symbol='BTCUSDT', side='LONG', signal_type='TREND_UP',
               source='S3', strategy='S6', timestamp=1788000000)
    assert s.timestamp == '2026-08-29T10:40:00.000Z'


def test_strength_coercion():
    """strength 数值化：'72'→72.0；不可解析→None（不猜）。"""
    s1 = Signal(symbol='X', side='LONG', signal_type='T', source='S3',
                strategy='S6', strength='72')
    assert s1.strength == 72.0
    s2 = Signal(symbol='X', side='LONG', signal_type='T', source='S3',
                strategy='S6', strength='abc')
    assert s2.strength is None


def test_unknown_source_open_set():
    """source 开放集合：未知来源字符串直接通过。"""
    s = Signal(symbol='X', side='LONG', signal_type='T', source='NEW_AI_ENGINE',
               strategy='S6')
    assert s.source == 'NEW_AI_ENGINE'


def test_invalid_timestamp_raises():
    """非法时间字符串 → 严格失败。"""
    with pytest.raises(SignalValidationError):
        Signal(symbol='X', side='LONG', signal_type='T', source='S3',
               strategy='S6', timestamp='not-a-time')


def test_signal_side_enum_matches_contract():
    """SignalSide 枚举值与归一化输出一致。"""
    for side in SignalSide:
        s = Signal(symbol='X', side=side.value, signal_type='T', source='S3', strategy='S6')
        assert s.side == side.value
