"""trade_analyzer 回归测试：落库字段与异步入队行为。"""

import json
import queue
from unittest.mock import patch

from shared import trade_analyzer as ta


class _DummyCHClient:
    def __init__(self):
        self.ddl = []

    def command(self, sql):
        self.ddl.append(sql)


class _DummyResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_analyze_closed_trade_inserts_clickhouse_row():
    ta._TABLE_READY = False
    klines = [
        [0, '100', '101', '99', '100', '1'],
        [0, '100', '112', '98', '110', '1'],
    ]
    ch = _DummyCHClient()

    with patch('shared.trade_analyzer.get_client', return_value=ch), \
         patch('shared.trade_analyzer._analysis_exists', return_value=False), \
         patch('shared.trade_analyzer.requests.get', return_value=_DummyResp(klines)), \
         patch('shared.trade_analyzer._ch_insert') as ins:
        ta.analyze_closed_trade(
            symbol='BTCUSDT',
            source='S6',
            side='LONG',
            entry=100.0,
            exit_price=110.0,
            qty=1.0,
            leverage=3,
            pct=10.0,
            pnl_usdt=10.0,
            duration_min=30,
            result='win',
            exit_reason='take_profit',
            signal_type='TREND_UP',
            score=80.0,
            market_state='trend',
            btc_trend='up',
            sl_price=95.0,
            open_time=1_700_000_000,
        )

    assert ch.ddl and 'CREATE TABLE IF NOT EXISTS default.trade_analysis' in ch.ddl[0]
    assert ins.called
    table, row = ins.call_args[0]
    assert table == 'default.trade_analysis'
    payload = json.loads(row)
    assert payload['symbol'] == 'BTCUSDT'
    assert payload['mfe_pct'] > 0
    assert payload['quality_tag'] in {'excellent', 'good', 'neutral', 'weak', 'bad'}


def test_analyze_closed_trade_t15_inserts_post_close_fields():
    ta._TABLE_READY = False
    ch = _DummyCHClient()

    with patch('shared.trade_analyzer.get_client', return_value=ch), \
         patch('shared.trade_analyzer._analysis_exists', return_value=False), \
         patch('shared.trade_analyzer.requests.get', return_value=_DummyResp({'price': '120'})), \
         patch('shared.trade_analyzer._ch_insert') as ins:
        ta.analyze_closed_trade(
            symbol='BTCUSDT',
            source='S6',
            side='LONG',
            entry=100.0,
            exit_price=110.0,
            qty=1.0,
            leverage=3,
            pct=10.0,
            pnl_usdt=10.0,
            duration_min=30,
            result='win',
            exit_reason='take_profit',
            signal_type='TREND_UP',
            score=80.0,
            market_state='trend',
            btc_trend='up',
            sl_price=95.0,
            open_time=1_700_000_000,
            close_ts=1_700_000_100,
            phase='T15',
            phase_delay_min=15,
        )

    payload = json.loads(ins.call_args[0][1])
    assert payload['phase'] == 'T15'
    assert payload['post_close_return_pct'] > 0
    assert payload['post_close_label'] in {'missed_follow_through', 'avoided_reversal', 'neutral_after_exit'}


def test_enqueue_closed_trade_returns_false_when_queue_full(monkeypatch):
    q = queue.PriorityQueue(maxsize=1)
    q.put((0, 1, {'x': 1}))
    monkeypatch.setattr(ta, '_Q', q)
    monkeypatch.setattr(ta, '_ensure_worker', lambda: None)
    assert ta.enqueue_closed_trade({'x': 2}) is False


def test_get_rollup_stats_reads_t0_and_post_metrics():
    with patch('shared.trade_analyzer._ensure_table', return_value=None), \
         patch('shared.trade_analyzer._ch_query', side_effect=[
             [[10, 1.2, 3.4, 0.8, 66.6, 6]],
             [[0.5, 1.1, 2, 3]],
         ]) as q:
        stats = ta.get_rollup_stats(symbol='BTCUSDT', event_type='TREND_UP', system_name='S6', lookback_days=14)

    assert stats['trades'] == 10
    assert stats['win_rate'] == 60.0
    assert stats['avg_pct'] == 1.2
    assert stats['avg_pnl_usdt'] == 3.4
    assert stats['avg_rr_realized'] == 0.8
    assert stats['avg_quality_score'] == 66.6
    assert stats['t15_avg_post_close_return_pct'] == 0.5
    assert stats['t60_avg_post_close_return_pct'] == 1.1
    assert stats['t15_missed_follow_through'] == 2
    assert stats['t60_missed_follow_through'] == 3
    assert q.call_count == 2


def test_get_rollup_stats_defaults_when_query_fails():
    with patch('shared.trade_analyzer._ensure_table', return_value=None), \
         patch('shared.trade_analyzer._ch_query', side_effect=Exception('down')):
        stats = ta.get_rollup_stats(symbol="X'Y", lookback_days=999)

    assert stats['lookback_days'] == 30
    assert stats['symbol'] == "X'Y"
    assert stats['trades'] == 0
    assert stats['win_rate'] == 0.0
    assert stats['t15_avg_post_close_return_pct'] == 0.0
