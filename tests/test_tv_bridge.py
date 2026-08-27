"""tv_bridge 回归测试：secret 校验 / 信号映射 / 去重 / 快照 / 信号源合并。"""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

_spec = importlib.util.spec_from_file_location('tv_bridge', _BASE / 'services' / 'tv_bridge.py')
tv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tv)


@pytest.fixture
def bridge(monkeypatch):
    """Redis 内存替身 + 固定 secret。"""
    store = {}

    def fake_get(key):
        return store.get(key)

    def fake_set(key, data):
        store[key] = data

    monkeypatch.setattr(tv, '_rget', fake_get)
    monkeypatch.setattr(tv, '_rset', fake_set)
    monkeypatch.setattr(tv, '_rpublish', lambda *a, **k: True)
    monkeypatch.setattr(tv, '_secret', lambda: 'test-secret')
    return {'store': store}


def _payload(**kw):
    base = {'secret': 'test-secret', 'signal': 'TREND_UP_LONG',
            'symbol': 'BTCUSDT', 'strength': 70, 'price': 61100.5}
    base.update(kw)
    return base


def test_secret_rejected(bridge):
    ok, msg = tv.process_alert(_payload(secret='wrong'))
    assert ok is False and 'secret' in msg


def test_unknown_signal_rejected(bridge):
    ok, msg = tv.process_alert(_payload(signal='MAGIC_CROSS'))
    assert ok is False and '未知信号' in msg


def test_bad_symbol_rejected(bridge):
    ok, _ = tv.process_alert(_payload(symbol='LTCUSD_PERP'))
    assert ok is False


def test_symbol_normalized(bridge):
    ok, _ = tv.process_alert(_payload(symbol='BINANCE:ETHUSDT.P'))
    assert ok is True
    ev = bridge['store'][tv.TV_EVENT_KEY]['events'][0]
    assert ev['symbol'] == 'ETHUSDT'


def test_signal_mapped_to_internal_type(bridge):
    ok, _ = tv.process_alert(_payload())
    assert ok is True
    ev = bridge['store'][tv.TV_EVENT_KEY]['events'][0]
    assert ev['type'] == 'TREND_UP'
    assert ev['side'] == 'LONG'
    assert ev['strength'] == 70
    assert ev['source'] == 'tv'


def test_strength_clamped(bridge):
    ok, _ = tv.process_alert(_payload(strength=250))
    assert ok is True
    ev = bridge['store'][tv.TV_EVENT_KEY]['events'][0]
    assert ev['strength'] == 99


def test_flow_fields_forwarded(bridge):
    ok, _ = tv.process_alert(_payload(taker_buy_ratio=0.62, orderflow_bias=0.18))
    assert ok is True
    ev = bridge['store'][tv.TV_EVENT_KEY]['events'][0]
    assert ev['taker_buy_ratio'] == 0.62
    assert ev['orderflow_bias'] == 0.18


def test_flow_fields_out_of_range_dropped(bridge):
    ok, _ = tv.process_alert(_payload(taker_buy_ratio=1.5, orderflow_bias=2.0))
    assert ok is True
    ev = bridge['store'][tv.TV_EVENT_KEY]['events'][0]
    assert 'taker_buy_ratio' not in ev
    assert 'orderflow_bias' not in ev


def test_dedup_window(bridge):
    now = time.time()
    assert tv.process_alert(_payload(), now=now)[0] is True
    ok, msg = tv.process_alert(_payload(), now=now + 60)
    assert ok is False and '去重' in msg
    # 不同标的不去重
    assert tv.process_alert(_payload(symbol='ETHUSDT'), now=now + 60)[0] is True
    # 窗口过后放行
    assert tv.process_alert(_payload(), now=now + tv.DEDUP_WINDOW_S + 1)[0] is True


def test_snapshot_window_and_cap(bridge):
    now = time.time()
    tv.process_alert(_payload(symbol='BTCUSDT'), now=now - 200)   # 过期
    tv.process_alert(_payload(symbol='ETHUSDT'), now=now)
    snap = bridge['store'][tv.TV_EVENT_KEY]
    assert len(snap['events']) == 1
    assert snap['events'][0]['symbol'] == 'ETHUSDT'


def test_read_tv_signals_and_merge(monkeypatch, bridge):
    from strategies import shared_executor as se

    now = time.time()
    tv.process_alert(_payload(), now=now)

    monkeypatch.setattr(se, 'read_s3_events', lambda: [{'type': 'TREND_DOWN', 'symbol': 'AAVEUSDT'}])
    monkeypatch.setattr(se, 'read_tv_signals', lambda: [{'type': 'TREND_UP', 'symbol': 'BTCUSDT'}])

    monkeypatch.delenv('SIGNAL_SOURCE', raising=False)
    merged = se.read_all_signals()
    assert {e['symbol'] for e in merged} == {'AAVEUSDT', 'BTCUSDT'}

    monkeypatch.setenv('SIGNAL_SOURCE', 'tv')
    assert {e['symbol'] for e in se.read_all_signals()} == {'BTCUSDT'}

    monkeypatch.setenv('SIGNAL_SOURCE', 's3')
    assert {e['symbol'] for e in se.read_all_signals()} == {'AAVEUSDT'}


def test_resolve_event_flow_prefers_event():
    from strategies.shared_executor import resolve_event_flow, resolve_event_orderflow_bias

    market = {'15m': {'taker_buy_ratio': 0.0, 'orderflow_bias': -1.0}}
    # 事件自带真实 flow → 用事件
    assert resolve_event_flow({'taker_buy_ratio': 0.65}, market) == 0.65
    # 事件没有 / 为 0 → 回退 market
    assert resolve_event_flow({}, market) == 0.0
    assert resolve_event_flow({'taker_buy_ratio': 0}, market) == 0.0
    # orderflow_bias 同理
    assert resolve_event_orderflow_bias({'orderflow_bias': 0.3}, market) == 0.3
    assert resolve_event_orderflow_bias({}, market) == -1.0


def test_symbol_pool_gate_forwards_only_in_pool(bridge):
    bridge['store']['market:s3_data'] = {'symbols': {'BTCUSDT': {}, 'ETHUSDT': {}}}
    # 在池内 → 转发
    assert tv.process_alert(_payload(symbol='BTCUSDT'))[0] is True
    # 不在池内 → 忽略
    ok, msg = tv.process_alert(_payload(symbol='AIOUSDT'))
    assert ok is False and '币池' in msg


def test_symbol_pool_empty_is_lenient(bridge):
    # 币池为空（S3 刚启动/不可用）→ 不阻断，交给执行器处理
    assert 'market:s3_data' not in bridge['store']
    assert tv.process_alert(_payload(symbol='SOMECOINUSDT'))[0] is True


def test_symbol_pool_can_be_disabled(monkeypatch, bridge):
    monkeypatch.setenv('TV_REQUIRE_IN_POOL', '0')
    bridge['store']['market:s3_data'] = {'symbols': {'BTCUSDT': {}}}
    assert tv.process_alert(_payload(symbol='AIOUSDT'))[0] is True
