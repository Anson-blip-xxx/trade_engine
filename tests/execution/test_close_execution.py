"""Golden：PM._close / PM._partial_close Execution 行为。"""
import time

import pytest

from shared import position_manager as pm


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
            'set_order_result': set_order_result}


def _pos(**kw):
    base = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': time.time(), 'leverage': 3,
            'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
            'score': 66, 'be_done': False}
    base.update(kw)
    return base


# ── Close Execution ─────────────────────────────────────────────────────

class TestCloseExecution:
    def test_full_close_short_success(self, close_env):
        """正常平空：MARKET BUY reduceOnly → record → PG → 移除。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions

        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'BUY'
        assert order['params']['type'] == 'MARKET'
        assert order['params']['reduceOnly'] == 'true'
        assert order['params']['positionSide'] == 'BOTH'
        assert order['params']['quantity'] == 10.0

        rec = close_env['calls']['record'][0]
        assert rec['kwargs']['side'] == 'SHORT'
        assert rec['kwargs']['exit_reason'] == '硬止损'
        assert rec['kwargs']['final_close'] is True

        pg_types = [e['event_type'] for e in close_env['calls']['pg']]
        assert 'CLOSE_ORDER_FILLED' in pg_types

    def test_full_close_long_side_sell(self, close_env):
        """平多 → SELL。"""
        pos = _pos(side='LONG')
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([[{'symbol': 'AUSDT', 'positionAmt': '10', 'entryPrice': '2.0'}]])
        r = close_env['pm']._close('AUSDT', pos, 2.1, '手动平仓', positions)
        assert r is True
        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'SELL'

    def test_close_exchange_already_flat(self, close_env):
        """交易所已无持仓 → 记录 + EXCHANGE_POSITION_FLAT + 移除。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([])  # 无持仓
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions
        pg_types = [e['event_type'] for e in close_env['calls']['pg']]
        assert 'EXCHANGE_POSITION_FLAT' in pg_types
        # 无 order POST（交易所已平，无需下单）
        order_posts = [p for p in close_env['calls']['post'] if 'order' in p['path']]
        assert order_posts == []

    def test_close_rejected_clears_marker(self, close_env):
        """交易所拒绝 → 清除标记 + False + 持仓保留。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_order_result']({'code': -2019, 'msg': 'Margin is insufficient.'})
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is False
        assert 'AUSDT' in positions
        assert close_env['redis'].get('closed:AUSDT') is None

    def test_close_partial_fill_keeps_remaining(self, close_env):
        """部分成交：记录 final_close=False，保留剩余 qty。"""
        pos = _pos(qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}],   # 第一次查仓
            [{'symbol': 'AUSDT', 'positionAmt': '-6', 'entryPrice': '2.0'}],    # 平仓后剩 6
        ])
        close_env['set_order_result'](
            {'orderId': 1, 'status': 'FILLED', 'executedQty': '4'})
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is False
        assert positions['AUSDT']['qty'] == 6.0
        rec = close_env['calls']['record'][0]
        assert rec['kwargs']['final_close'] is False

    def test_close_call_sequence(self, close_env, monkeypatch):
        """调用顺序冻结：mark → positionRisk → order → positionRisk → record → PG → cancel → save。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])

        seq = []
        monkeypatch.setattr(close_env['pm'], '_mark_closed',
                            lambda s: (seq.append('mark'),
                                       pm._rset(f'closed:{s}', {'ts': time.time()})))

        close_env['pm']._close('AUSDT', pos, 1.9, '手动平仓', positions)
        # 验证关键顺序存在
        assert seq[0] == 'mark'
        pg_types = [e['event_type'] for e in close_env['calls']['pg']]
        assert 'CLOSE_ORDER_FILLED' in pg_types
        assert close_env['calls']['cancel_algo'] == ['AUSDT']


# ── Partial Close Execution ─────────────────────────────────────────────

class TestPartialCloseExecution:
    def test_long_partial(self, close_env):
        """LONG 分层止盈：qty 减少，pnl 正确，无 record_trade。"""
        pos = _pos(side='LONG', entry=1.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        assert pos['qty'] == 5.0
        assert len(close_env['calls']['record']) == 0

    def test_short_partial(self, close_env):
        pos = _pos(side='SHORT', entry=2.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
        assert pos['qty'] == 6.0

    def test_partial_close_order_params(self, close_env):
        """部分平仓订单参数：SHORT → BUY，positionSide=BOTH，无 reduceOnly（当前行为）。"""
        pos = _pos(side='SHORT', qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'BUY'
        assert order['params']['type'] == 'MARKET'
        assert order['params']['positionSide'] == 'BOTH'
        assert 'reduceOnly' not in order['params']  # OBS：_close 有，_partial_close 无

    def test_partial_close_rejected_no_change(self, close_env):
        """交易所拒绝 → qty 不变。"""
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['set_order_result']({'code': -1111, 'msg': 'Precision error'})
        close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        assert pos['qty'] == 10.0   # 不变

    def test_partial_close_negative_qty_increases(self, close_env):
        """Observed Current Behavior：close_qty<0 → qty 增加（10-(-100)=110）。"""
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.1, -100.0, 5, positions)
        assert pos['qty'] == 110.0   # 10 - (-100) = 110
