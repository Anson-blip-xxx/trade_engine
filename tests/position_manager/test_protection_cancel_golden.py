"""P7-04A：cancel-id + cancel-all golden（fallback API + 状态过滤）。"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def algo_spy(monkeypatch):
    events = {'get': [], 'post': [], 'delete': [], 'info': []}

    def light_get(path, params=None):
        events['get'].append((path, params))
        if 'allAlgoOrders' in path:
            return events.get('allAlgoOrders', [])
        if 'algoOrder' in path:
            return events.get('algoOrderGerman', [])
        return None
    monkeypatch.setattr(pm, '_light_fapi_get', light_get)
    monkeypatch.setattr(pm, '_light_fapi_post',
                        lambda p, q=None: events['post'].append((p, q)))
    monkeypatch.setattr(pm, '_light_fapi_delete',
                        lambda p, q=None: events['delete'].append((p, q)))
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    # s6api fapi_delete normal path (Binance via binance_api)
    def s6api_delete(path, params=None):
        events['delete'].append((path, params, 'normal'))
        return {'status': 'CANCELED sessionFactory'}
    monkeypatch.setattr(pm, '_s6api', lambda: (
        lambda p, q=None: None, lambda p, q=None: None, s6api_delete,
        lambda s: 1.0, lambda s: (6, 6), lambda *a, **k: (0, 0, 0),
        lambda *a, **k: 50.0, lambda *a, **kw: None))
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    return {'pm': pm, 'events': events}


class TestCancelId:
    def test_s6api_mode_delete_correct_path(self, algo_spy):
        """正常 s6api 模式（binance_api fapi_delete）→ DELETE 而非 GET。"""
        algo_spy['pm']._algo_cancel(12345)
        deletes = algo_spy['events']['delete']
        assert deletes == [('/fapi/v1/algoOrder', {'algoId': 12345}, 'normal')]

    def test_s6api_mode_returns_raw(self, algo_spy):
        algo_spy['events']['delete'] = []
        raw = algo_spy['pm']._algo_cancel(42)
        assert isinstance(raw, dict)

    def test_fallback_mode_get_not_delete_frozen(self, algo_spy, monkeypatch):
        """PMB-9 冻结：fallback tuple 第三槽 = `_light_fapi_get` →
        `_algo_cancel` 经兜底模式发 GET 而非 DELETE（不修，只冻结）。"""
        my_pm = algo_spy['pm']
        monkeypatch.setattr(my_pm, '_s6api', lambda: (
            my_pm._light_fapi_get, my_pm._light_fapi_post,
            my_pm._light_fapi_get,          # 第三槽 = fapi_get（PMB-9）
            lambda s: 1.0, lambda s: (6, 6),
            lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0,
            lambda *a, **kw: None))
        r = my_pm._algo_cancel(12345)
        # fallback path: GET → 200 status_code? light_fapi_get returns list or None
        # result = GET response — test catches shape only
        assert r == []                               # GET response 冻结 (list 非 dict)

    def test_exception_wraps_error(self, algo_spy, monkeypatch):
        def boom_delete(*a, **k):
            raise RuntimeError('delete down')
        monkeypatch.setattr(algo_spy['pm'], '_s6api', lambda: (
            None, None, boom_delete, None, None, None, None, None))
        r = algo_spy['pm']._algo_cancel(1)
        assert r == {'error': 'delete down'}


class TestCancelAll:
    def test_filters_by_algo_status_frozen(self, algo_spy):
        """status ∈ (NEW, WORKING, TRIGGERED) 才取消；其余 skip。"""
        pm = algo_spy['pm']
        algo_spy['events']['allAlgoOrders'] = [
            {'algoId': 1, 'algoStatus': 'NEW', 'triggerPrice': 0.9},
            {'algoId': 2, 'algoStatus': 'FINISHED', 'triggerPrice': 0.9},  # 跳过
            {'algoId': 3, 'algoStatus': 'WORKING'},
            {'algoId': 4, 'algoStatus': 'TRIGGERED'},
            {'algoId': 5, 'algoStatus': 'EXPIRED'},                        # 跳过
        ]
        pm._cancel_all_algo('AUSDT')
        deletes = [d for d in algo_spy['events']['delete'] if 'algoOrder' in d[0]]
        deleted_ids = [d[1]['algoId'] for d in deletes]
        assert deleted_ids == [1, 3, 4]              # 2/5 跳过（status filter 冻结）

    def test_cancel_all_before_place_order_frozen(self, algo_spy, monkeypatch):
        """cancel-all 在 place 之前执行（P7-04A 冻结顺序）。"""
        order = []
        pm = algo_spy['pm']
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: order.append('cancel_all'))
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda p, q=None: order.append('place') or {'algoId': 1})
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, q=None: [])
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert order == ['cancel_all', 'place']

    def test_cancel_all_failure_swallowed(self, algo_spy, monkeypatch):
        """cancel-all 失败 → 吞错 + log（不抛、不终止 worker/placement）。"""
        def boom(path, params=None):
            raise RuntimeError('del down')
        algo_spy['pm']._light_fapi_get = boom
        algo_spy['pm']._cancel_all_algo('AUSDT')     # 不抛
