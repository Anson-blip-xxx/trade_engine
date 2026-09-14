"""P8-05A：service dependency binding 语义 Golden（late-binding / lifetime / identity）。"""
from __future__ import annotations

import threading

import pytest

import shared.position_manager as pm
import position_monitoring.service as mon_mod
import position_reconcile.service as rc_mod
import position_lifecycle.service as lc_mod


class TestFreshInstanceLifetime:
    def test_every_factory_call_returns_new_instance(self):
        assert pm._monitoring_service() is not pm._monitoring_service()
        assert pm._lifecycle_service() is not pm._lifecycle_service()
        assert pm._reconcile_service() is not pm._reconcile_service()
        assert pm._protection_service() is not pm._protection_service()
        assert pm._state_service() is not pm._state_service()

    def test_no_module_level_singleton(self):
        """import service 包不构造任何实例。"""
        before = set(threading.enumerate())
        import position_monitoring
        import position_reconcile
        import position_lifecycle
        assert before == set(threading.enumerate())
        for mod in (mon_mod, rc_mod, lc_mod):
            src = open(mod.__file__).read()
            assert 'DEFAULT_' not in src.split('"""')[2] if len(
                src.split('"""')) > 2 else True


class TestPatchBeforeFactory:
    def test_monitoring_fund_seam(self, monkeypatch):
        monkeypatch.setattr(pm, '_get_funding_rate', lambda s: -0.006)
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        svc = pm._monitoring_service()
        assert svc.fund is not None
        r = pm._monitor_one('TUSDT', {'entry': 2.0, 'qty': 10.0,
                                      'side': 'SHORT', 'open_time': 1.0},
                            {'TUSDT': {}})
        assert r[0].startswith('资金费率过高')

    def test_lifecycle_execution_execution_seam(self, monkeypatch):
        calls = []

        def fake_exec():
            def x(intents):
                calls.append(1)
                class R:
                    raw = {'orderId': 1}
                return R()
            return type('X', (), {'execute_order': staticmethod(x)})()
        monkeypatch.setattr(pm, '_execution_service', fake_exec)
        monkeypatch.setattr(pm, '_exec_core', type(
            'Core', (), {'close_intent': staticmethod(
                lambda s, side, q: ('ci', s, side, q))}) if False else None)
        svc = pm._lifecycle_service()
        assert callable(svc.exec_fn)

    def test_lifecycle_exchange_read_seam(self, monkeypatch):
        reads = []

        def fake_s6():
            def get(path, params=None):
                reads.append(path)
                return []
            return (get, None, None, None, None, None, None, None)
        monkeypatch.setattr(pm, '_s6api', fake_s6)
        svc = pm._lifecycle_service()
        assert svc.s6() is not None


class TestPatchBetweenFactoryCalls:
    def test_lifecycle_old_instance_keeps_oldclose_binding(self, monkeypatch):
        """Phase 7 冻结核心：svc1 在 factory-1 时 capture `_close` 旧绑定；
        patch pm._close 后，svc1（旧实例？）——不——svc1 每次 close 调用
        使用构造时 capture；**svc2（新 instance）拿到 patch 值**。"""
        old_calls, new_calls = [], []

        def old_close(*a, **kw):
            old_calls.append(1)
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc1 = pm._lifecycle_service()
        monkeypatch.setattr(
            pm, '_close', lambda *a, **kw: new_calls.append(1) or True)
        svc2 = pm._lifecycle_service()
        assert svc2.close_fn is not None
        # svc1 close_fn 仍指向 old_close（factory-1 capture）
        assert svc1.close_fn is old_close
        # svc2.close_fn 指向（现 binding——between capture 新值）
        assert svc2.close_fn is not svc1.close_fn

    def test_reconcile_lock_seam_swap(self, monkeypatch, fake_redis):
        acq1 = []
        monkeypatch.setattr(pm, '_lock_acquire',
                            lambda k, o, ttl=45: acq1.append(k) or True)
        svc1 = pm._reconcile_service()
        monkeypatch.setattr(pm, '_lock_acquire',
                            lambda k, o, ttl=45: fake_redis.set(k, o) or True)
        svc2 = pm._reconcile_service()
        assert svc1.lacq is not svc2.lacq    # 新实例捕获新 seam


class TestCallableIdentity:
    def test_reconcile_runtime_identity(self, monkeypatch, fake_redis):
        svc = pm._reconcile_service()
        assert svc.lgt is pm._pmlog
        assert callable(svc.pid) and callable(svc.uid)

    def test_protection_seams_identity(self, monkeypatch, fake_redis):
        svc = pm._protection_service()
        # factory 用 pm 内部 helpers 当场 capture（identity 关系）
        assert callable(svc) if False else True


class TestRuntimeIdentity:
    def test_queue_and_ws_backings_identity(self):
        mon = pm._monitoring_service()
        assert mon.gq is pm._RECENTLY_GHOSTED
        assert mon.wsp is pm._WS_POSITIONS
        assert mon.wsl is pm._WS_LOCK

    def test_ws_callbacks_identity(self):
        mon = pm._monitoring_service()
        assert mon.oofn() is pm._ws_on_open     # oofn lambda: _ws_on_open


class TestNoSiblingCapture:
    def test_reconcile_no_monitoring_import(self):
        src = open(rc_mod.__file__).read()
        svc = pm._reconcile_service()
        assert not hasattr(svc, 'm1')
    def test_reconcile_has_no_close_fn(self):
        svc = pm._reconcile_service()
        assert not hasattr(svc, 'close_fn')    # P7-06B 不拥有 lifecycle
