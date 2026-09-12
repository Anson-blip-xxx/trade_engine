"""P5-01：S3 ↔ 消费方 / Unified Signal / TV 兼容 golden（contract-only 冻结）。"""
import json
import time

import pytest


# ═══════════════════════════════════════════════════════════════
#  真实 S3 事件样例（与 detect_events 输出一致的结构）
# ═══════════════════════════════════════════════════════════════

S3_EVENT = {'type': 'PULSE_DOWN', 'symbol': 'DOGEUSDT', 'strength': 55,
            'chg_15m': -5.2, 'chg_1h': -3.0, 'state': 'ACTIVE',
            'since': 1700000000.0}
S3_SLICE = {'ts': 1700000010.0, 'events': [S3_EVENT]}


# ═══════════════════════════════════════════════════════════════
#  shared_executor 消费侧
# ═══════════════════════════════════════════════════════════════

class TestSharedExecutorConsumer:
    def test_read_s3_events_fields_and_stale_gate(self, monkeypatch):
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: S3_SLICE)
        monkeypatch.setattr(se, 'time', type('T', (), {
            'time': staticmethod(lambda: 1700000050.0)}))  # fresh gate
        out = se.read_s3_events()
        assert out == [dict(S3_EVENT, _snapshot_ts=1700000010.0)]

    def test_read_s3_events_snapshot_stale_empty(self, monkeypatch):
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: S3_SLICE)
        monkeypatch.setattr(se, 'time', type('T', (), {
            'time': staticmethod(lambda: 1700000200.0)}))  # stale
        out = se.read_s3_events(max_age=90)
        assert out == []

    def test_read_s3_events_redis_error_empty(self, monkeypatch):
        from strategies import shared_executor as se
        def boom(k):
            raise RuntimeError('down')
        monkeypatch.setattr(se, '_rget', boom)
        assert se.read_s3_events() == []

    def test_read_all_signals_merges_s3_phonly(self, monkeypatch):
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: S3_SLICE)
        monkeypatch.setattr(se, 'time', type('T', (), {
            'time': staticmethod(lambda: 1700000050.0)}))
        monkeypatch.setenv('SIGNAL_SOURCE', 's3')
        out = se.read_all_signals()
        assert [e['type'] for e in out] == ['PULSE_DOWN']

    def test_event_expected_move_mapping(self, monkeypatch):
        from strategies import shared_executor as se
        # PULSE/PUMP/PANIC → chg_15m；TREND → chg_1h；VIOLENT_* → vol_1h
        assert se._event_expected_move({'type': 'PULSE_DOWN',
                                        'chg_15m': -5.2}) == 5.2
        assert se._event_expected_move({'type': 'TREND_UP',
                                        'chg_1h': 12.0}) == 12.0
        assert se._event_expected_move({'type': 'VIOLENT_BULLISH',
                                        'vol_1h': 18}) == 18.0
        assert se._event_expected_move({'type': 'HIGH_VOL'}) == 0


# ═══════════════════════════════════════════════════════════════
#  Unified Signal adapter（现有 contract 冻结，不新增行为）
# ═══════════════════════════════════════════════════════════════

class TestUnifiedSignalCompatibility:
    def test_s6_signal_fields(self):
        from signals.adapters import s6_signal
        sig = s6_signal(S3_EVENT)
        assert sig.symbol == 'DOGEUSDT'
        assert sig.side == 'LONG'                 # side 由消费方注入
        assert sig.signal_type == 'PULSE_DOWN'    # S3 事件类型即 signal_type
        assert sig.source == 'S3'
        assert sig.strategy == 'S6'
        assert sig.strength == 55
        assert sig.timestamp is None              # S3 事件层无 ts (S3-5)
        assert sig.event_id is None               # S3 无 event_id
        assert sig.metadata['chg_15m'] == -5.2    # 原字段保留进 metadata

    def test_s8_signal_side_injection(self):
        from signals.adapters import s8_signal
        sig = s8_signal(S3_EVENT)
        assert sig.side == 'SHORT'
        assert sig.strategy == 'S8'

    def test_to_journal_builder_kwargs_values(self):
        from signals.adapters import to_journal_builder_kwargs
        from signals.adapters import s6_signal
        sig = s6_signal(S3_EVENT)
        kwargs = to_journal_builder_kwargs(sig)
        assert kwargs['signal_source'] == 'S3'
        assert kwargs['signal_type'] == 'PULSE_DOWN'
        assert kwargs['strength'] == 55
        assert kwargs['event_id'] is None
        assert kwargs['raw']['state'] == 'ACTIVE'   # raw = metadata（原事件）

    def test_tv_event_carries_side_field_s3_does_not(self):
        """S3 vs TV contract 差异冻结（不统一）：仅 TV 有 side/tv_signal。"""
        tv_event = {'type': 'PULSE_DOWN', 'symbol': 'DOGEUSDT',
                    'strength': 55, 'ts': 1700000010.0,
                    'side': 'SHORT', 'tv_signal': 'short'}
        assert 'side' not in S3_EVENT            # S3 无 side（类型后缀承载方向）
        assert tv_event['side'] == 'SHORT'
        # Signal 侧：TV 事件 timestamp/token 保留：metadata['side']
        from signals.adapters import s8_signal
        sig = s8_signal(tv_event)
        assert sig.metadata['side'] == 'SHORT'   # 原始字段进 metadata


# ═══════════════════════════════════════════════════════════════
#  begin-to-end：compute_and_detect → read_s3_events 完整兼容
# ═══════════════════════════════════════════════════════════════

def test_end_to_end_s3_snapshot_consumer_roundtrip(s3m, redis_spy, monkeypatch):
    """真实链路：S3 process内写 event:s3 → se.read_s3_events 读出为兼容事件列表。"""
    import strategies.s3_orderflow as s3
    import strategies.shared_executor as se

    n = 120
    klines = [{'t': i, 'o': 100.0, 'h': 100.1, 'l': 99.9, 'c': 100.0,
               'v': 10.0, 'tbv': 0.5} for i in range(n)]
    klines = sorted(klines, key=lambda x: -x['t'])
    for i in range(3):        # sorted 后首 3 = 最新 → 高比量跳出
        klines[i]['v'] = 60.0
    s3m._symbol_klines['BTCUSDT'] = klines

    s3m.compute_and_detect(['BTCUSDT'])
    snapshot = redis_spy.store['event:s3']
    monkeypatch.setattr(se, '_rget', lambda k: snapshot)
    monkeypatch.setattr(se, 'time', type('T', (), {
        'time': staticmethod(lambda: snapshot['ts'] + 10)}))

    out = se.read_s3_events()
    assert isinstance(out, list)
    assert all('type' in e and 'symbol' in e and 'strength' in e
               for e in out)                     # required 字段齐
    assert all('_snapshot_ts' in e for e in out)
    # 高 vol 事件应触发 HIGH_VOL（真实事件）
    assert any(e['type'] in {'HIGH_VOL', 'VIOLENT_BULLISH', 'VIOLENT_BEARISH',
                             'PULSE_UP', 'PULSE_DOWN'} for e in out)
