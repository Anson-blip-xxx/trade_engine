"""Signal Adapter 测试：事件 dict → Unified Signal → Journal 映射边界。"""
import pytest

from journal import DecisionJournalBuilder
from signals import (
    SignalValidationError,
    s6_signal,
    s8_signal,
    to_journal_builder_kwargs,
    validate_signal,
)

T = 1788000000

S3_EVT = {'symbol': 'TUTUSDT', 'type': 'TREND_DOWN', 'strength': 72,
          'chg_15m': -2.8, 'chg_1h': -6.5, 'chg_4h': -9.2,
          '_snapshot_ts': 1788302380.0}

TV_EVT = {'type': 'TREND_DOWN', 'symbol': 'TUTUSDT', 'strength': 72,
          'ts': T, 'source': 'tv', 'tv_signal': 'TREND_DOWN_SHORT',
          'side': 'SHORT', 'price': 0.043,
          'taker_buy_ratio': 0.38, 'orderflow_bias': -0.24}


# ── S3 事件适配 ──────────────────────────────────────────────────────────

def test_s3_event_to_signal_via_s6():
    s = s6_signal(S3_EVT)
    assert s.symbol == 'TUTUSDT'
    assert s.side == 'LONG'
    assert s.signal_type == 'TREND_DOWN'
    assert s.source == 'S3'
    assert s.strategy == 'S6'
    assert s.strength == 72.0
    assert s.timestamp is None                    # S3 事件无 ts，如实为 None
    assert s.metadata['chg_1h'] == -6.5           # 原始字段保留在 metadata
    assert validate_signal(s) == []


def test_s3_event_to_signal_via_s8():
    s = s8_signal(S3_EVT)
    assert s.side == 'SHORT' and s.strategy == 'S8'


def test_s3_event_fields_not_upgraded():
    """市场指标（chg/flow）不进 Signal 核心字段——保留在 metadata。"""
    s = s6_signal(S3_EVT)
    assert not hasattr(s, 'chg_1h')
    assert not hasattr(s, 'atr')
    assert s.metadata['_snapshot_ts'] == 1788302380.0


# ── TradingView 事件适配 ─────────────────────────────────────────────────

def test_tv_event_to_signal():
    s = s8_signal(TV_EVT)
    assert s.source == 'TRADINGVIEW'
    assert s.signal_type == 'TREND_DOWN'
    assert s.side == 'SHORT'
    assert s.timestamp == '2026-08-29T10:40:00.000Z'   # TV ts → ISO
    assert s.metadata['tv_signal'] == 'TREND_DOWN_SHORT'
    assert validate_signal(s) == []


def test_tv_side_kept_in_metadata_consumer_side_wins():
    """TV 事件自带 side，但消费方注入的 side 优先；TV side 保留在 metadata。"""
    s = s6_signal(dict(TV_EVT, side='SHORT'))       # TV 说是 SHORT
    assert s.side == 'LONG'                          # 消费方 S6 = LONG（当前行为）
    assert s.metadata['side'] == 'SHORT'


# ── 严格失败 ─────────────────────────────────────────────────────────────

def test_missing_symbol_raises():
    with pytest.raises(SignalValidationError):
        s6_signal({'type': 'TREND_UP', 'strength': 70})


def test_non_dict_event_raises():
    with pytest.raises(SignalValidationError):
        s6_signal('not-a-dict')


# ── 确定性与纯度 ─────────────────────────────────────────────────────────

def test_adapter_deterministic():
    a = s6_signal(S3_EVT)
    b = s6_signal(dict(S3_EVT))
    assert a == b


def test_adapter_does_not_mutate_input():
    import copy
    evt = copy.deepcopy(S3_EVT)
    s6_signal(evt)
    assert evt == S3_EVT


# ── Journal 映射边界（Signal → Builder kwargs） ──────────────────────────

def test_to_journal_builder_kwargs_mapping():
    s = s8_signal(S3_EVT)
    kw = to_journal_builder_kwargs(s)
    assert kw['signal_source'] == 'S3'
    assert kw['signal_type'] == 'TREND_DOWN'
    assert kw['symbol'] == 'TUTUSDT'
    assert kw['side'] == 'SHORT'
    assert kw['strength'] == 72.0
    assert kw['signal_timestamp'] is None            # S3 无 ts → None（不变）
    assert kw['event_id'] is None
    assert kw['strategy'] == 'S8'
    assert kw['raw']['chg_1h'] == -6.5               # 原始事件完整保留


def test_journal_builder_output_matches_p1_02_semantics():
    """映射边界：Signal → Builder kwargs → SignalSnapshot 与 P1-02 直连方式产出一致。"""
    s = s8_signal(S3_EVT)
    jb = DecisionJournalBuilder(**to_journal_builder_kwargs(s))
    j = jb.build(action='REJECT', accepted=False, reason='test')
    assert j.signal.source == 'S3'
    assert j.signal.symbol == 'TUTUSDT'
    assert j.signal.side == 'SHORT'
    assert j.signal.signal_type == 'TREND_DOWN'
    assert j.signal.strength == 72.0
    assert j.signal.raw['chg_1h'] == -6.5


def test_tv_signal_timestamp_flows_to_journal():
    """TV 事件带 ts → journal signal_timestamp 有值（有意的行为完善，见 MAPPING）。"""
    s = s8_signal(TV_EVT)
    jb = DecisionJournalBuilder(**to_journal_builder_kwargs(s))
    j = jb.build(action='OPEN', accepted=True)
    assert j.signal.signal_timestamp == '2026-08-29T10:40:00.000Z'


def test_adapter_strict_on_invalid_ts():
    """Adapter 契约：非法输入严格抛错；fail-open 由集成边界（S6/S8）负责。"""
    with pytest.raises(SignalValidationError):
        s6_signal(dict(S3_EVT, ts='xxx-invalid-ts'))
