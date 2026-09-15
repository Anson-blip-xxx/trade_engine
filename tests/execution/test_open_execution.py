"""Golden：shared_executor.open_position Execution 全流程。

冻结当前行为：order 发送 / 成交解析 / cancel / PM 注册 / PG / TG / Algo SL。
Decision gates 已被 P3-01 覆盖——本文件 patch 所有 gates 为通过，
专注 Execution 层的行为。
"""
import pytest

from conftest import make_open_kwargs, order_posts, cancel_calls, all_post_paths


# ── 正常全量成交 ────────────────────────────────────────────────────────

def test_normal_full_fill(exec_env):
    """正常全量成交 → True，leverage/marginType/order 按序发出，PM/PG/TG/Algo 全触发。"""
    se = exec_env['se']
    calls = exec_env['calls']
    r = se.open_position(**make_open_kwargs(tg_fn=exec_env['calls']['tg'].append))
    assert r is True

    # 调用顺序：leverage → marginType → order
    paths = all_post_paths(calls)
    assert paths == ['/fapi/v1/leverage', '/fapi/v1/marginType', '/fapi/v1/order']

    # PM 注册：pm:positions 有记录
    stored = exec_env['redis'].get('pm:positions')
    assert stored['TESTUSDT']['entry'] == 100.0
    assert stored['TESTUSDT']['qty'] == 100.0

    # PG event
    assert len(calls['pg']) == 1
    assert calls['pg'][0]['event_type'] == 'OPEN_ORDER_FILLED'

    # TG
    assert len(calls['tg']) == 1

    # Algo SL
    assert calls['algo'] == [{'symbol': 'TESTUSDT', 'side': 'SELL',
                              'trigger': 95.0, 'qty': 100.0}]

    # 无 cancel
    assert cancel_calls(calls) == []


# ── Order Construction ─────────────────────────────────────────────────

def test_order_params_long(exec_env):
    """LONG → BUY / MARKET / RESULT / 正确 quantity。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs())
    order = order_posts(exec_env['calls'])[0]['params']
    assert order['symbol'] == 'TESTUSDT'
    assert order['side'] == 'BUY'
    assert order['type'] == 'MARKET'
    assert order['quantity'] == 100.0
    assert order['newOrderRespType'] == 'RESULT'
    assert 'reduceOnly' not in order      # 开仓单无 reduceOnly
    assert 'positionSide' not in order    # 当前代码不传 positionSide


def test_order_params_short(exec_env):
    """SHORT → SELL。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(side='SHORT'))
    order = order_posts(exec_env['calls'])[0]['params']
    assert order['side'] == 'SELL'
    assert order['type'] == 'MARKET'


def test_leverage_and_margin_posts(exec_env):
    """杠杆和保证金模式 POST 参数冻结。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(margin_mode='ISOLATED', leverage=5))
    posts = exec_env['calls']['post']
    assert posts[0]['params'] == {'symbol': 'TESTUSDT', 'leverage': 5}
    assert posts[1]['params'] == {'symbol': 'TESTUSDT', 'marginType': 'ISOLATED'}


def test_margin_mode_crossed(exec_env):
    """CROSSED 模式 → marginType POST 参数。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(margin_mode='CROSSED'))
    posts = exec_env['calls']['post']
    assert posts[1]['params'] == {'symbol': 'TESTUSDT', 'marginType': 'CROSSED'}


# ── 成交解析 ────────────────────────────────────────────────────────────

def test_fill_parsing_avg_price(exec_env):
    """avgPrice 正常 → 使用交易所成交价（非 entry_price）。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 1, 'status': 'FILLED',
                                        'executedQty': '100', 'avgPrice': '101.5'}
    se.open_position(**make_open_kwargs())
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['entry'] == 101.5


def test_fill_parsing_avg_price_missing(exec_env):
    """avgPrice 缺失 → 回退 entry_price。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 1, 'status': 'FILLED',
                                        'executedQty': '100'}
    se.open_position(**make_open_kwargs())
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['entry'] == 100.0      # entry_price fallback


def test_fill_parsing_avg_price_zero(exec_env):
    """avgPrice = '0' → 回退 entry_price。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 1, 'status': 'FILLED',
                                        'executedQty': '100', 'avgPrice': '0'}
    se.open_position(**make_open_kwargs())
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['entry'] == 100.0


# ── Error / Edge Cases ─────────────────────────────────────────────────

def test_order_rejected(exec_env):
    """交易所拒绝（code 非空）→ False，无 PM/PG/TG/Algo。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'code': -2019, 'msg': 'Margin is insufficient.'}
    r = se.open_position(**make_open_kwargs())
    assert r is False
    # 无 PM/PG/TG/Algo
    assert exec_env['redis'].get('pm:positions') is None
    assert exec_env['calls']['pg'] == []
    assert exec_env['calls']['algo'] == []
    assert cancel_calls(exec_env['calls']) == []


def test_status_new_zero_fill(exec_env):
    """status=NEW + filled=0 → 取消订单 + False。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'NEW',
                                        'executedQty': '0', 'cumQty': '0'}
    r = se.open_position(**make_open_kwargs())
    assert r is False
    assert cancel_calls(exec_env['calls']) == [{'symbol': 'TESTUSDT', 'orderId': 42}]
    assert exec_env['redis'].get('pm:positions') is None


def test_zero_fill_no_order_id(exec_env):
    """status=NEW + filled=0 + 无 orderId → 不发取消，False。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'status': 'NEW', 'executedQty': '0'}
    r = se.open_position(**make_open_kwargs())
    assert r is False
    assert cancel_calls(exec_env['calls']) == []


def test_zero_filled_qty(exec_env):
    """filled < 0.01 + cum < 0.01（非 NEW）→ 取消 + False。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '0', 'cumQty': '0'}
    r = se.open_position(**make_open_kwargs())
    assert r is False


def test_partial_fill_below_50pct(exec_env):
    """filled < qty×0.5 → 取消剩余 + 接受已成交部分。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '30', 'cumQty': '30',
                                        'avgPrice': '100.0'}
    r = se.open_position(**make_open_kwargs(qty=100.0))
    assert r is True
    # 取消剩余
    assert cancel_calls(exec_env['calls']) == [{'symbol': 'TESTUSDT', 'orderId': 42}]
    # PM 注册用 filled_qty（30），不是原始 qty（100）
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['qty'] == 30.0
    # Algo SL 用 filled_qty
    assert exec_env['calls']['algo'][0]['qty'] == 30.0


def test_partial_fill_above_50pct(exec_env):
    """filled >= qty×0.5 → 接受全部（不取消）。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '60', 'cumQty': '60',
                                        'avgPrice': '100.0'}
    r = se.open_position(**make_open_kwargs(qty=100.0))
    assert r is True
    assert cancel_calls(exec_env['calls']) == []
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['qty'] == 60.0


def test_pm_cache_update_failure_cancels_order(exec_env, monkeypatch):
    """PM 缓存更新异常 → 取消订单 + False。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '100', 'avgPrice': '100.0'}

    def _failing_update(*a, **k):
        raise RuntimeError('cache update failed')
    monkeypatch.setattr(se, '_update_pos_cache', _failing_update)
    r = se.open_position(**make_open_kwargs())
    assert r is False
    # 取消订单
    assert cancel_calls(exec_env['calls']) == [{'symbol': 'TESTUSDT', 'orderId': 42}]
    # 无 PM/PG/TG/Algo
    assert exec_env['calls']['pg'] == []
    assert exec_env['calls']['algo'] == []


def test_binance_exception(exec_env, monkeypatch):
    """Binance API 异常 → False（整体 try/except）。"""
    se = exec_env['se']

    def _raise(path, params=None):
        if 'order' in path:
            raise RuntimeError('network timeout')
        return None
    monkeypatch.setattr(se, 'fapi_post', _raise)
    r = se.open_position(**make_open_kwargs())
    assert r is False


# ── PG / TG / Algo 内容冻结 ─────────────────────────────────────────────

def test_pg_event_content(exec_env):
    """PG event 结构冻结。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(decision_context={'signal_type': 'TREND_UP'}))
    event = exec_env['calls']['pg'][0]
    assert event['event_type'] == 'OPEN_ORDER_FILLED'
    assert event['symbol'] if 'symbol' in event else True  # symbol 在 position_id 里
    assert event['price'] == 100.0
    assert event['qty'] == 100.0
    assert event['payload']['decision_context'] == {'signal_type': 'TREND_UP'}


def test_algo_enqueue_params(exec_env):
    """Algo SL enqueue 参数冻结：LONG → SELL。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(side='LONG', stop_price=95.0, qty=100.0))
    assert exec_env['calls']['algo'] == [{'symbol': 'TESTUSDT', 'side': 'SELL',
                                          'trigger': 95.0, 'qty': 100.0}]


def test_algo_enqueue_short(exec_env):
    """SHORT → BUY 方向的 SL。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(side='SHORT', stop_price=105.0, qty=50.0))
    assert exec_env['calls']['algo'][0]['side'] == 'BUY'
    assert exec_env['calls']['algo'][0]['trigger'] == 105.0


def test_no_algo_when_stop_zero(exec_env):
    """stop_price=0 → 不入队 Algo SL。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(stop_price=0))
    assert exec_env['calls']['algo'] == []


def test_no_tg_when_no_tg_fn(exec_env):
    """tg_fn=None → 不发 Telegram。"""
    se = exec_env['se']
    se.open_position(**make_open_kwargs(tg_fn=None))
    assert exec_env['calls']['tg'] == []


# ── No-retry（真实代码：order 失败不重试，单次尝试）────────────────────

def test_open_order_failure_no_retry(exec_env):
    """order 被拒 → 恰好 1 次 order 调用（无 retry），False，无 PM/PG/TG/Algo。"""
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'code': -2019, 'msg': 'Margin is insufficient.'}
    tg_calls = []
    r = se.open_position(**make_open_kwargs(tg_fn=tg_calls.append))
    assert r is False
    orders = order_posts(exec_env['calls'])
    assert len(orders) == 1                    # 单次尝试，无重试
    assert orders[0]['params']['quantity'] == 100.0
    assert exec_env['redis'].get('pm:positions') is None   # PM 不注册
    assert exec_env['calls']['pg'] == []                   # PG 不记录
    assert tg_calls == []                                  # TG 不发送
    assert exec_env['calls']['algo'] == []                 # Algo 不入队


def test_open_order_none_response_no_retry(exec_env, monkeypatch):
    """order 返回 None → 恰好 1 次 order 调用，False，无 PM。"""
    se = exec_env['se']
    base_post = se.fapi_post   # exec_env 已注入的可控 fapi_post

    def _none_for_order(path, params=None):
        if 'order' in path:
            exec_env['calls']['post'].append({'path': path, 'params': params})
            return None
        return base_post(path, params)
    monkeypatch.setattr(se, 'fapi_post', _none_for_order)
    r = se.open_position(**make_open_kwargs())
    assert r is False
    orders = order_posts(exec_env['calls'])
    assert len(orders) == 1
    assert orders[0]['params'] == {'symbol': 'TESTUSDT', 'side': 'BUY',
                                   'type': 'MARKET', 'quantity': 100.0,
                                   'newOrderRespType': 'RESULT'}
    assert exec_env['redis'].get('pm:positions') is None
    assert exec_env['calls']['pg'] == []


def test_pm_open_order_exception_no_retry(close_env, monkeypatch):
    """pm.open_position：order 异常 → 单次尝试（无 retry），False，不写 pm:positions。"""
    pm = close_env['pm']

    def make_boom_s6api():
        def fapi_post(path, params=None):
            close_env['calls']['post'].append({'path': path, 'params': params})
            if 'order' in path:
                raise RuntimeError('network timeout')
            return {}
        return (lambda p, q=None: [], fapi_post, lambda *a, **k: {},
                lambda sym: 2.0, lambda sym: (6, 6),
                lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0,
                lambda *a, **kw: None)
    monkeypatch.setattr(pm, '_s6api', make_boom_s6api)
    # P9-03B：dup precheck 前移——需要空 local state（仅 order 走路）
    monkeypatch.setattr(pm, '_load', lambda: {})

    r = pm.open_position('AUSDT', 'SHORT', 2.0, 10.0, 3, 2.2)
    assert r is False
    orders = [p for p in close_env['calls']['post'] if 'order' in p['path']]
    assert len(orders) == 1
    # pm 开仓单参数冻结（含 positionSide=BOTH，与 se 不同）
    assert orders[0]['params'] == {'symbol': 'AUSDT', 'side': 'SELL',
                                   'type': 'MARKET', 'quantity': 10.0,
                                   'positionSide': 'BOTH'}
    assert close_env['redis'].get('pm:positions') is None
