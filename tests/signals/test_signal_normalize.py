"""Signal 归一化规则测试（deterministic 纯函数）。"""
import pytest

from signals import (
    SignalValidationError,
    normalize_side,
    normalize_signal_type,
    normalize_source,
    normalize_strategy,
    normalize_strength,
    normalize_symbol,
    normalize_timestamp,
)


def test_side_buy_to_long():
    """BUY → LONG（Execution 词汇由 Adapter 转换）。"""
    assert normalize_side('BUY') == 'LONG'
    assert normalize_side('buy') == 'LONG'
    assert normalize_side(' Buy ') == 'LONG'


def test_side_sell_to_short():
    assert normalize_side('SELL') == 'SHORT'
    assert normalize_side('sell') == 'SHORT'


def test_side_case_and_space_insensitive():
    assert normalize_side(' long ') == 'LONG'
    assert normalize_side('SHORT') == 'SHORT'


def test_side_none_empty_to_unknown():
    assert normalize_side(None) == 'UNKNOWN'
    assert normalize_side('') == 'UNKNOWN'


def test_side_unknown_raises():
    with pytest.raises(SignalValidationError):
        normalize_side('UP')
    with pytest.raises(SignalValidationError):
        normalize_side('MEGA_LONG')


def test_symbol_normalized():
    assert normalize_symbol(' btcusdt ') == 'BTCUSDT'
    assert normalize_symbol(None) == ''


def test_signal_type_normalized():
    assert normalize_signal_type(' trend_up ') == 'TREND_UP'
    assert normalize_signal_type(None) == ''


def test_source_tv_mapping():
    assert normalize_source('tv') == 'TRADINGVIEW'
    assert normalize_source('TRADINGVIEW') == 'TRADINGVIEW'
    assert normalize_source('s3') == 'S3'
    assert normalize_source('') == 'UNKNOWN'
    assert normalize_source('future') == 'FUTURE'   # 开放集合


def test_strategy_normalized():
    assert normalize_strategy(' s6 ') == 'S6'
    assert normalize_strategy('S8') == 'S8'


def test_strength_normalization():
    assert normalize_strength(70) == 70.0
    assert normalize_strength('72.5') == 72.5
    assert normalize_strength(None) is None
    assert normalize_strength('abc') is None


def test_timestamp_normalization():
    assert normalize_timestamp(1788000000) == '2026-08-29T10:40:00.000Z'
    assert normalize_timestamp('2026-08-29T10:40:00.000Z') == '2026-08-29T10:40:00.000Z'
    assert normalize_timestamp(None) is None
    assert normalize_timestamp('') is None


def test_timestamp_invalid_raises():
    with pytest.raises(SignalValidationError):
        normalize_timestamp('not-a-time')
    with pytest.raises(SignalValidationError):
        normalize_timestamp(True)


def test_normalization_deterministic():
    """同一输入 → 同一输出（无 locale/时间/随机依赖）。"""
    for _ in range(10):
        assert normalize_side(' buy ') == 'LONG'
        assert normalize_source('tv') == 'TRADINGVIEW'
        assert normalize_timestamp(1788000000) == '2026-08-29T10:40:00.000Z'
