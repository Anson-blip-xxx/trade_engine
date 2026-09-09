"""Golden：_round_qty + Sandbox 拦截行为。"""
import json

import pytest

from strategies import shared_executor as se


# ── _round_qty ──────────────────────────────────────────────────────────

class TestRoundQty:
    def test_lot_size_step(self, monkeypatch):
        """exchangeInfo LOT_SIZE stepSize → 按精度舍入。"""
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01000000'}]}]}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456) == 1.23

    def test_lot_size_step_1(self, monkeypatch):
        """stepSize=1 → 向下取整到整数（qty % step 截断，非四舍五入）。"""
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '1.00000000'}]}]}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 17508.9) == 17508.0

    def test_exchange_info_exception_fallback(self, monkeypatch):
        """exchangeInfo 异常 → 返回原始 qty（当前行为，不优化）。"""
        def fapi_get(path, params=None):
            raise RuntimeError('network error')
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456789) == 1.23456789

    def test_exchange_info_missing_symbol(self, monkeypatch):
        """symbol 不在 exchangeInfo 中 → 返回原始 qty。"""
        info = {'symbols': []}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456789) == 1.23456789


# ── Sandbox ─────────────────────────────────────────────────────────────

class TestSandboxExecutor:
    def test_sandbox_check_off(self, monkeypatch):
        """沙盘关闭 → fapi_post 正常调用（返回 None→不开仓）。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: False)
        assert se._sandbox_check() is False

    def test_sandbox_check_on(self, monkeypatch):
        """沙盘开启 → _sandbox_check True。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        assert se._sandbox_check() is True

    def test_sandbox_post_intercept(self, monkeypatch):
        """沙盘开启 → fapi_post 被拦截返回 mock 结果。"""
        mock_result = {'orderId': 999, 'status': 'FILLED'}
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        monkeypatch.setattr(se, '_sandbox_post',
                            lambda path, params: mock_result)
        r = se.fapi_post('/fapi/v1/order', {'symbol': 'X'})
        assert r == mock_result


class TestSandboxPM:
    def test_pm_sandbox_active_false(self, monkeypatch):
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        assert pm._sandbox_active() is False

    def test_pm_sandbox_active_true(self, monkeypatch):
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: True)
        assert pm._sandbox_active() is True

    def test_pm_close_sandbox_mode(self, monkeypatch, fake_redis):
        """PM._close 沙盘模式 → 不调 Binance，直接记录。"""
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: True)
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
        monkeypatch.setattr(pm, '_pg_record_event', lambda *a, **k: None)

        record_calls = []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None,
            lambda *a, **kw: record_calls.append({'args': a, 'kwargs': kw})))

        pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
               'system': 'S8', 'open_time': 1788000000, 'leverage': 3,
               'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
               'score': 66, 'be_done': False}
        positions = {'AUSDT': pos}
        r = pm._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions
        assert len(record_calls) == 1
        # 沙盘路径无 order POST
        # pnl = (2.0 - 1.9) * 10 = 1.00（SHORT 正确方向）


# ── 真实 Sandbox interception（不 mock fapi_post / _sandbox_post）──────

class TestRealSandboxInterception:
    """验证 shared_executor 当前真实 fapi_post sandbox 拦截。

    方法：只 fake 最底层 requests.post / requests.get（真实网络请求），
    被测的 fapi_post / _sandbox_post / scripts.sandbox.mock_post_order 全走真实实现。
    """

    @pytest.fixture
    def net_spy(self, monkeypatch, tmp_path):
        import requests
        from scripts import sandbox as sb

        calls = {'post': [], 'get': []}

        def fake_post(url, params=None, headers=None, timeout=None):
            calls['post'].append({'url': url, 'params': dict(params or {}),
                                  'headers': dict(headers or {})})
            return type('Resp', (), {
                'status_code': 200, 'json': staticmethod(lambda: {'net': 'ok'})})()

        def fake_get(url, params=None, headers=None, timeout=None):
            calls['get'].append({'url': url})
            return type('Resp', (), {
                'status_code': 200, 'json': staticmethod(lambda: {'net': 'ok'})})()

        monkeypatch.setattr(requests, 'post', fake_post)
        monkeypatch.setattr(requests, 'get', fake_get)
        # 沙盘状态文件 → tmp（不触碰真实 sandbox_state.json）
        monkeypatch.setattr(sb, 'STATE_FILE', tmp_path / 'sandbox_state.json')
        # 喂价避免真实行情（urllib ticker）
        monkeypatch.setattr(sb, '_PRICE_CACHE', {})
        sb.seed_price('TESTUSDT', 100.0)
        # mock orderId 重置为可预测
        monkeypatch.setattr(sb, '_mock_order_id', 10000000)
        return {'calls': calls, 'sb': sb, 'state_file': tmp_path / 'sandbox_state.json'}

    def _sandbox_on(self, monkeypatch):
        """开沙盘：se 开关 + scripts.sandbox 环境变量（两者独立检查）。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        monkeypatch.setenv('SANDBOX', '1')

    def test_sandbox_on_order_intercepted(self, net_spy, monkeypatch):
        """Sandbox ON + /fapi/v1/order → 真实拦截，返回 mock 成交，零网络请求。"""
        self._sandbox_on(monkeypatch)
        r = se.fapi_post('/fapi/v1/order', {
            'symbol': 'TESTUSDT', 'side': 'BUY', 'type': 'MARKET', 'quantity': 10.0})
        # 沙盘 mock 成交结果（mock_post_order 真实实现产生）
        assert r['status'] == 'FILLED'
        assert r['executedQty'] == '10.0'
        assert r['avgPrice'] == '100.0'   # 来自 seed_price，非真实行情
        assert r['orderId'] == 10000001   # _mock_order_id(重置 10000000) + 1
        # 未触达网络层
        assert net_spy['calls']['post'] == []
        assert net_spy['calls']['get'] == []
        # 沙盘状态已写入（tmp 状态文件）
        state = json.loads(net_spy['state_file'].read_text())
        assert len(state['positions']) == 1
        assert state['positions'][0]['symbol'] == 'TESTUSDT'
        assert state['positions'][0]['positionAmt'] == 10.0

    def test_sandbox_on_algo_order_intercepted(self, net_spy, monkeypatch):
        """Sandbox ON + /fapi/v1/algoOrder（path 含 'order'）→ 同样被拦截。"""
        self._sandbox_on(monkeypatch)
        r = se.fapi_post('/fapi/v1/algoOrder', {
            'symbol': 'TESTUSDT', 'side': 'SELL', 'algoType': 'CONDITIONAL',
            'type': 'STOP_MARKET', 'triggerPrice': 95.0, 'quantity': 10.0,
            'reduceOnly': 'true'})
        assert r['algoId'] == 10000001
        assert r['status'] == 'NEW'
        assert net_spy['calls']['post'] == []   # 未触网
        state = json.loads(net_spy['state_file'].read_text())
        assert len(state['algo_orders']) == 1
        assert state['algo_orders'][0]['triggerPrice'] == 95.0
        assert state['algo_orders'][0]['reduceOnly'] is True

    def test_sandbox_on_non_order_passthrough(self, net_spy, monkeypatch):
        """Sandbox ON + 非 order 路径（leverage）→ 不拦截，走真实网络层（已 fake）。"""
        self._sandbox_on(monkeypatch)
        r = se.fapi_post('/fapi/v1/leverage', {'symbol': 'TESTUSDT', 'leverage': 3})
        assert r == {'net': 'ok'}
        assert len(net_spy['calls']['post']) == 1
        sent = net_spy['calls']['post'][0]
        assert sent['url'].endswith('/fapi/v1/leverage')
        assert sent['headers']['X-MBX-APIKEY'] == se._API_KEY
        # 真实路径附加签名参数
        assert 'timestamp' in sent['params']
        assert 'signature' in sent['params']

    def test_sandbox_off_network_path(self, net_spy, monkeypatch):
        """Sandbox OFF → 正常网络层（已 fake），附加 timestamp/signature；
        且入参 dict 被原地附加签名（副作用冻结）。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: False)
        params = {'symbol': 'TESTUSDT', 'side': 'BUY', 'type': 'MARKET',
                  'quantity': 10.0}
        r = se.fapi_post('/fapi/v1/order', params)
        assert r == {'net': 'ok'}
        assert len(net_spy['calls']['post']) == 1
        sent = net_spy['calls']['post'][0]
        assert sent['url'].endswith('/fapi/v1/order')
        assert sent['params']['symbol'] == 'TESTUSDT'
        assert 'timestamp' in sent['params']
        assert 'signature' in sent['params']
        # 副作用：调用方传入的 dict 被原地修改
        assert 'signature' in params
        assert 'timestamp' in params
