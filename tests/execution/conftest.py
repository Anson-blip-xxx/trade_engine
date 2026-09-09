"""Execution characterization 共享夹具（P4-01）。

隔离原则：只替换 IO（Binance API / Redis / PG / Telegram / Algo 队列线程）；
被测 Execution 函数（shared_executor.open_position / PM._close / PM._partial_close）
全部走真实实现。
"""
import time

import pytest

from journal.recorder import MemoryJournalRecorder


@pytest.fixture
def exec_env(monkeypatch, fake_redis):
    """shared_executor.open_position 执行环境。

    - 所有 Decision/Risk gates 默认通过（专注于 Execution 行为）
    - Binance API 可控（order_result 可覆盖）
    - PM 注册真实执行（写 fake_redis pm:positions）
    - Algo SL 队列捕获（不启动真实 worker）
    - PG 事件捕获
    """
    from strategies import shared_executor as se

    calls = {'post': [], 'get': [], 'cancel': [], 'algo': [], 'pg': [], 'tg': []}
    ctrl = {'order_result': None}   # 设为非 None 时覆盖默认订单响应
    ctrl['existing_position'] = []  # positionRisk 返回值

    monkeypatch.setattr(se, '_log', lambda *a, **k: None)

    # Redis
    monkeypatch.setattr(se, '_rget', fake_redis.get)
    monkeypatch.setattr(se, '_rset', fake_redis.set)

    # PG
    monkeypatch.setattr(se, '_pg_record_event', lambda event: calls['pg'].append(event))

    # Algo SL
    monkeypatch.setattr(se, '_algo_start_worker', lambda: None)

    def _enqueue(symbol, side, trigger, qty):
        calls['algo'].append({'symbol': symbol, 'side': side,
                              'trigger': trigger, 'qty': qty})
    monkeypatch.setattr(se, '_algo_enqueue', _enqueue)

    # Decision/Risk gates → 全部通过（专注 Execution）
    monkeypatch.setattr(se, '_analysis_allows_open',
                        lambda *a, **k: (True, '', 1.0))
    monkeypatch.setattr(se, '_drawdown_status', lambda: (1.0, 0.0))
    monkeypatch.setattr(se, '_was_closed_recently',
                        lambda sym, within_hours=4: False)
    monkeypatch.setattr(se, '_get_min_notional', lambda sym: 5.0)
    monkeypatch.setattr(se, '_get_funding_rate', lambda sym: 0.0001)

    # Binance API
    def fapi_get(path, params=None):
        calls['get'].append(path)
        if 'positionRisk' in path:
            return ctrl.get('existing_position', [])
        return None
    monkeypatch.setattr(se, 'fapi_get', fapi_get)

    def fapi_post(path, params=None):
        calls['post'].append({'path': path, 'params': params})
        if 'cancel' in path:
            calls['cancel'].append(params)
            return {'orderId': 1}
        if 'order' in path:
            if ctrl['order_result'] is not None:
                return ctrl['order_result']
            qty = params.get('quantity', 0) if isinstance(params, dict) else 0
            return {'orderId': 1, 'status': 'FILLED',
                    'executedQty': str(qty), 'cumQty': str(qty),
                    'avgPrice': '100.0'}
        return {}
    monkeypatch.setattr(se, 'fapi_post', fapi_post)

    # _round_qty → identity（默认；测试可覆盖）
    monkeypatch.setattr(se, '_round_qty', lambda sym, qty: qty)

    # TG
    def _tg(msg):
        calls['tg'].append(msg)
    monkeypatch.setattr(se, 'tg_send', _tg)

    # 重置 _POS_CACHE（防跨测试污染）
    monkeypatch.setattr(se, '_POS_CACHE', {})

    return {'se': se, 'calls': calls, 'ctrl': ctrl, 'redis': fake_redis}


def make_open_kwargs(**kw):
    """open_position 标准调用参数。"""
    base = {'name': 'S6', 'symbol': 'TESTUSDT', 'side': 'LONG',
            'entry_price': 100.0, 'stop_price': 95.0, 'qty': 100.0,
            'margin_mode': 'CROSSED', 'leverage': 3,
            'event_type': 'TREND_UP', 'strength': 70, 'tg_fn': None,
            'expected_move_pct': 0, 'decision_context': {}}
    base.update(kw)
    return base


def order_posts(calls):
    """从 post 调用中筛选出 order 请求。"""
    return [p for p in calls['post'] if p['path'] == '/fapi/v1/order']


def cancel_calls(calls):
    return calls['cancel']


def all_post_paths(calls):
    return [p['path'] for p in calls['post']]
