"""P9-02A：PMB-9 characterization（cancel fallback GET bug 冻结；勿修）。"""
from __future__ import annotations

import pytest

import shared.position_manager as pm


class TestTupleSlotContract:
    def test_primary_tuple_slots_correct(self):
        """primary tuple（fresh import edge case 不可控）——这里只冻结
        兜底 tuple 的 contract。"""
        src = open('shared/position_manager.py').read()
        # 兜底 tuple 第三 slot 语义 = fapi_delete
        assert '_S6_API = (_light_fapi_get, _light_fapi_post, _light_fapi_get,' in src

    def test_intended_slot_should_be_delete(self):
        """intended：第三 slot = _light_fapi_delete（P9-02B 目标），当前不是。"""
        src = open('shared/position_manager.py').read()
        assert "def _light_fapi_delete" in src

    def test_fallback_slot_identity_reproducible(self, monkeypatch):
        """真实 fallback tuple（shared.binance_api import 失败时构建）
        第三 slot = `_light_fapi_g`et — 通过现成 cached fallback tuple
        （模块回到 fallback 路径）＋ reflection：`_s6api()[2] is _light_fapi_get`。"""
        # 强制回退路径：patch import 依赖使 binance_api 导入链失败
        import shared.binance_api as ba
        keep = dict(ba.__dict__)
        try:
            class Boom:
                def __getattr__(self, k):
                    raise ImportError('no binance_api')
            ba.__dict__.clear()
            ba.__dict__.update({'__name__': 'shared.binance_api'})
            pm._S6_API = None
            tuple8 = pm._s6api()
            # ── 核心 PMB-9 bug reproduction ──
            assert tuple8[0] is pm._light_fapi_get   # slot1 OK
            assert tuple8[1] is pm._light_fapi_post  # slot2 OK
            # slot3 intended DELETE；当前是 GET（PMB-9）
            assert tuple8[2] is pm._light_fapi_get   # ← BUG
            assert tuple8[2] is not pm._light_fapi_delete
        finally:
            ba.__dict__.clear()
            ba.__dict__.update(keep)
            pm._S6_API = None


class TestAlgoCancelPrimary:
    def test_algo_cancel_primary_route(self, monkeypatch):
        """primary：`_s6api()[2]` → fapi_delete('/fapi/v1/algoOrder',
        {'algoId': id})；成功时无 fallback 调用任何 light IO。"""
        calls = {'get': [], 'delete': []}

        def fake_get(path, params=None):
            calls['get'].append(path)
            return []
        fake_delete = lambda path, params=None: (
            calls['delete'].append((path, params)) or {'ok': 1})
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, fake_delete := fake_delete_holder, None, None,
            None, None, None))

        def fake_delete_holder(path, params=None):
            return fake_delete(path, params)
        r = pm._algo_cancel(321)
        assert r == {'ok': 1}
        assert calls == {'get': [], 'delete': [('/fapi/v1/algoOrder',
                                               {'algoId': 321})]}

    def test_algo_cancel_exception_return_error(self, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError('cancel down')
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, boom, None, None, None, None, None))
        r = pm._algo_cancel(1)
        assert 'error' in r


class TestCancelAllStatusFilter:
    def test_status_filter_frozen(self, monkeypatch):
        """PMB-15 冻结复确认：只有 NEW/WORKING/TRIGGERED 进 DELETE。"""
        fired = []
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [
            {'algoId': 1, 'algoStatus': 'NEW',
             'triggerPrice': 0.9},
            {'symbol': 'TUSDT', 'algoId': 2, 'algoStatus': 'WORKING',
             'triggerPrice': 0.9},
            {'symbol': 'TUSDT', 'algoId': 3, 'algoStatus': 'TRIGGERED',
             'triggerPrice': 0.9},
            {'symbol': 'TUSDT', 'algoId': 4, 'algoStatus': 'FINISHED',
             'triggerPrice': 0.9},
            {'symbol': 'TUSDT', 'algoId': 5, 'algoStatus': 'EXPIRED',
             'triggerPrice': 0.9},
        ])
        monkeypatch.setattr(pm, '_light_fapi_delete',
                            lambda p, params=None: fired.append(params) or {})
        monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
        pm._cancel_all_algo('TUSDT')
        ids = [p['algoId'] for p in fired]
        assert set(ids) == {1, 2, 3}             # FINISHED/EXPIRED skip (PMB-15)

    def test_cancel_all_endpoint_and_params_frozen(self, monkeypatch):
        fired = []
        get_calls = []
        monkeypatch.setattr(pm, '_light_fapi_get',
                            lambda p, params=None:
                            get_calls.append((p, params)) or
                            [{'symbol': 'TUSDT', 'algoId': 9,
                              'algoStatus': 'NEW'}])
        monkeypatch.setattr(pm, '_light_fapi_delete',
                            lambda p, params=None: fired.append((p, params))
                            or {})
        monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
        pm._cancel_all_algo('TUSDT')
        assert get_calls == [('/fapi/v1/allAlgoOrders',
                              {'symbol': 'TUSDT'})]
        assert fired == [('/fapi/v1/algoOrder',
                          {'symbol': 'TUSDT', 'algoId': 9})]

    def test_cancel_all_exception_logged(self, monkeypatch):
        def boom(p, params=None):
            raise RuntimeError('net')
        monkeypatch.setattr(pm, '_light_fapi_get', boom)
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(str(m)))
        pm._cancel_all_algo('TUSDT')
        assert any('批量取消异常' in l for l in logs)
        assert any('net' in l for l in logs)
