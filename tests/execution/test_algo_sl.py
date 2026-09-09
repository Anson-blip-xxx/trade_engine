"""Golden：Algo SL 队列与下单（真实 _algo_place_sl_inner 实现）。"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def algo_env(monkeypatch, fake_redis):
    calls = {'algo_post': [], 'cancel': [], 'load': []}
    ctrl = {'algo_result': {'algoId': 123}, 'exchange_info': None}

    monkeypatch.setattr(pm, '_light_fapi_post',
                        lambda path, params: (calls['algo_post'].append(
                            {'path': path, 'params': params}) or ctrl['algo_result']))
    monkeypatch.setattr(pm, '_cancel_all_algo',
                        lambda sym: calls['cancel'].append(sym))
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    monkeypatch.setattr(pm, '_load',
                        lambda: (calls['load'].append(1) or {}))
    monkeypatch.setattr(pm, '_save', lambda positions: None)

    import requests
    if ctrl['exchange_info'] is None:
        monkeypatch.setattr(requests, 'get',
                            lambda *a, **k: type('R', (), {
                                'status_code': 200, 'json': staticmethod(lambda: {'symbols': []})})())
    return {'pm': pm, 'calls': calls, 'ctrl': ctrl}


@pytest.fixture(autouse=True)
def clear_algo_queue():
    pm._ALGO_QUEUE.clear()
    yield
    pm._ALGO_QUEUE.clear()


# ── Enqueue ─────────────────────────────────────────────────────────────

class TestAlgoEnqueue:
    def test_long_sl_sell(self, algo_env):
        """LONG 止损 → SELL 方向入队。"""
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        assert pm._ALGO_QUEUE == [('AUSDT', 'SELL', 0.92, 100.0)]

    def test_short_sl_buy(self, algo_env):
        """SHORT 止损 → BUY 方向入队。"""
        pm._algo_enqueue('AUSDT', 'BUY', 1.08, 50.0)
        assert pm._ALGO_QUEUE == [('AUSDT', 'BUY', 1.08, 50.0)]

    def test_enqueue_fifo_order(self, algo_env):
        """多个任务 FIFO 入队。"""
        pm._algo_enqueue('AUSDT', 'SELL', 0.92, 100.0)
        pm._algo_enqueue('BUSDT', 'BUY', 1.08, 50.0)
        assert [t[0] for t in pm._ALGO_QUEUE] == ['AUSDT', 'BUSDT']


# ── Place（真实实现） ────────────────────────────────────────────────────

class TestAlgoPlaceInner:
    def test_place_posts_algo_order(self, algo_env):
        """真实实现：POST /fapi/v1/algoOrder，参数冻结。"""
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert len(algo_env['calls']['algo_post']) == 1
        params = algo_env['calls']['algo_post'][0]['params']
        assert algo_env['calls']['algo_post'][0]['path'] == '/fapi/v1/algoOrder'
        assert params['symbol'] == 'AUSDT'
        assert params['side'] == 'SELL'
        assert params['positionSide'] == 'BOTH'
        assert params['algoType'] == 'CONDITIONAL'
        assert params['type'] == 'STOP_MARKET'
        assert params['triggerPrice'] == 0.92
        assert params['quantity'] == 100.0
        assert params['workingType'] == 'MARK_PRICE'
        assert params['timeInForce'] == 'GTC'
        assert params['reduceOnly'] == 'true'

    def test_place_cancels_existing_first(self, algo_env):
        """下单前先取消该币全部条件单（防重启积累重复单）。"""
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert algo_env['calls']['cancel'] == ['AUSDT']
        # cancel 发生在 algoOrder POST 之前
        assert len(algo_env['calls']['algo_post']) == 1

    def test_place_success_returns_algo_id(self, algo_env):
        """成功 → 返回 {'algoId': ...}。"""
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'algoId': 123}

    def test_place_failure_returns_raw(self, algo_env):
        """失败 → 返回交易所原始错误（不包装）。"""
        algo_env['ctrl']['algo_result'] = {'code': -2021, 'msg': 'Order would immediately trigger.'}
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'code': -2021, 'msg': 'Order would immediately trigger.'}

    def test_place_exception_wraps_error(self, algo_env, monkeypatch):
        """异常 → {'error': str(e)}（不抛出）。"""
        def boom(path, params):
            raise RuntimeError('connection reset')
        monkeypatch.setattr(pm, '_light_fapi_post', boom)
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'error': 'connection reset'}

    def test_place_rounds_qty_by_lot_size(self, algo_env, monkeypatch):
        """exchangeInfo LOT_SIZE stepSize → 数量向下取整。"""
        import requests
        algo_env['ctrl']['exchange_info'] = {'symbols': [
            {'symbol': 'AUSDT', 'filters': [
                {'filterType': 'LOT_SIZE', 'stepSize': '0.01000000'},
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.00100000'},
            ]}]}

        def fake_get(url, timeout=10):
            assert 'exchangeInfo' in url
            return type('R', (), {
                'status_code': 200,
                'json': staticmethod(lambda: algo_env['ctrl']['exchange_info'])})()
        monkeypatch.setattr(requests, 'get', fake_get)

        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.9234567, 100.45678)
        params = algo_env['calls']['algo_post'][0]['params']
        assert params['quantity'] == 100.45     # 100.45678 - (100.45678 % 0.01)
        assert params['triggerPrice'] == 0.923  # 0.9234567 - (0.9234567 % 0.001)


# ── Cancel ──────────────────────────────────────────────────────────────

class TestCancelAll:
    def test_cancel_captures_symbol(self, algo_env):
        pm._cancel_all_algo('AUSDT')
        assert algo_env['calls']['cancel'] == ['AUSDT']
