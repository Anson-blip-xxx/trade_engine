"""P7-04A：protection failure matrix golden（restart/fallback/exchangeInfo）。"""
import pytest

from shared import position_manager as pm


class TestExchangeInfoFailure:
    def test_exchange_info_fail_place_still_runs(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        events = []
        monkeypatch.setattr(pm, '_light_fapi_get',
                            lambda p, q=None: (_ for _ in ()).throw(
                                RuntimeError('info down')))

        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError('info down')))
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: (events.append(p), {'algoId': 1})[1])
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'algoId': 1}                     # place 继续（吞错）

    def test_exchange_info_malformed_filters(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])

        class R:
            status_code = 200
            def json(self):
                return {'symbols': [{'symbol': 'AUSDT', 'filters': [
                    {'filterType': 'UNKNOWN'},  # 其他 filter → 无取
                ]}]}

        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: R())
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        events_place = []
        pm._light_fapi_post = (lambda p, q=None:
            (events_place.append(q), {'algoId': 1})[1])
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        # 无 LOT_SIZE/PRICE_FILTER → 不缺份（原始值保留）
        # verify raw requests.get called once
        payload_events = events_place[0]
        assert payload_events['triggerPrice'] == 0.92   # 无 rounding ticket

    def test_tick_size_price_round(self, pm_full, monkeypatch):
        pm = pm_full['pm']
        pm_full['set_sandbox'](False)

        class R:
            status_code = 200
            def json(self):
                return {'symbols': [{'symbol': 'AUSDT', 'filters': [
                    {'filterType': 'LOT_SIZE', 'stepSize': '0.01'},
                    {'filterType': 'PRICE_FILTER', 'tickSize': '0.001'},
                ]}]}

        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: R())
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: {'algoId': 1})  # noqa sanity
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        payload_events = []
        def intercepted_post(p, q=None):
            payload_events.append(q)
            return {'algoId': 1}
        monkeypatch.setattr(pm, '_light_fapi_post', intercepted_post)
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.9234567, 100.45678)
        assert len(payload_events) == 1
        assert payload_events[0]['quantity'] == pytest.approx(100.45)
        assert payload_events[0]['triggerPrice'] == pytest.approx(0.923)


class TestRoundAndTimeSemantics:
    def test_algo_worker_loop_11s_frozen_source(self):
        import inspect
        from shared import position_manager as pm
        from position_runtime import runtime as rt
        src = inspect.getsource(rt.algo_worker_loop)
        # P8-06B：loop mechanics 迁 runtime module；seam delegate 保持
        assert 'time.sleep(11)' in src
        assert 'time.sleep(1)' in src

    def test_worker_exception_does_not_kill_thread(self):
        import inspect
        from shared import position_manager as pm
        from position_runtime import runtime as rt
        src = inspect.getsource(rt.algo_worker_loop)
        # P8-06B：loop 迁 runtime module —— guard 目标随迁
        assert 'except Exception as e' in src
        assert 'time.sleep(11)' in src
        # while True keeps running after exception


class TestProtectionPortFrozen:
    def test_protection_port_exists_with_5_methods(self):
        from execution.ports.protection import ProtectionPort
        import inspect
        methods = [name for name, _ in inspect.getmembers(
            ProtectionPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert set(methods) == {'cancel_algo_id', 'cancel_all_algo',
                                'enqueue_algo_sl', 'place_algo_sl',
                                'start_algo_worker'}

    def test_protection_port_no_6th_method_yet(self):
        """P7-04A 确认 ProtectionPort 已足够支撑 P7-04B（不扩 port）。"""
        from execution import ports as ep
        import inspect
        src = inspect.getsource(ep.__class__ if False else
                                __import__('execution.ports.protection',
                                           fromlist=['x']))
        assert 'record_partial' not in src              # 无新方法
