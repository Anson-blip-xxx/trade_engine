"""Execution characterization 共享夹具（P4-01）。

隔离原则：只替换 IO（Binance API / Redis / PG / Telegram / Algo 队列线程）；
被测 Execution 函数（shared_executor.open_position / PM._close / PM._partial_close）
全部走真实实现。
"""
import time

import pytest

from journal.recorder import MemoryJournalRecorder

from shared import position_manager as pm


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



@pytest.fixture
def close_env(monkeypatch, fake_redis):
    """PM._close / _partial_close 执行环境。"""
    calls = {'post': [], 'get': [], 'record': [], 'pg': [], 'cancel_algo': []}
    risk_responses = [[{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}]]
    risk_reads = {'n': 0}

    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    monkeypatch.setattr(pm, '_pg_record_event', lambda event: calls['pg'].append(event))
    monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: calls['cancel_algo'].append(sym))

    def fapi_get(path, params=None):
        calls['get'].append(path)
        if 'positionRisk' in path:
            risk_reads['n'] += 1
            if risk_reads['n'] <= len(risk_responses):
                return risk_responses[risk_reads['n'] - 1]
            return []
        return None

    def fapi_post(path, params=None):
        calls['post'].append({'path': path, 'params': params})
        if 'order' in path:
            qty = params.get('quantity', 0) if isinstance(params, dict) else 0
            return {'orderId': 1, 'status': 'FILLED', 'executedQty': str(qty)}
        return {}

    def record_trade(*a, **kw):
        calls['record'].append({'args': a, 'kwargs': kw})

    def make_s6api():
        return (fapi_get, fapi_post, lambda *a, **k: {},
                lambda sym: 2.0, lambda sym: (6, 6),
                lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0, record_trade)

    monkeypatch.setattr(pm, '_s6api', make_s6api)
    monkeypatch.setattr(pm, '_light_fapi_get', fapi_get)
    monkeypatch.setattr(pm, '_light_fapi_post', fapi_post)

    override = {'order_result': None}

    def set_order_result(result):
        override['order_result'] = result

    def fapi_post_wrapped(path, params=None):
        r = fapi_post(path, params)
        if 'order' in path and override['order_result'] is not None:
            return override['order_result']
        return r
    monkeypatch.setattr(pm, '_light_fapi_post', fapi_post_wrapped)

    def make_s6api_wrapped():
        return (fapi_get, fapi_post_wrapped, lambda *a, **k: {},
                lambda sym: 2.0, lambda sym: (6, 6),
                lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0, record_trade)
    monkeypatch.setattr(pm, '_s6api', make_s6api_wrapped)

    def set_risk_responses(responses):
        risk_responses.clear()
        risk_responses.extend(responses)
        risk_reads['n'] = 0

    def set_sandbox(active):
        monkeypatch.setattr(pm, '_sandbox_active', lambda: active)

    set_sandbox(False)
    return {'pm': pm, 'redis': fake_redis, 'calls': calls,
            'set_risk_responses': set_risk_responses, 'set_sandbox': set_sandbox,
            'set_order_result': set_order_result,
            'fapi_get': fapi_get, 'fapi_post': fapi_post_wrapped}

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
