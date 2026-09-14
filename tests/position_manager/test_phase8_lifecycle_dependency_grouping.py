"""P8-05B1：LifecycleService constructor grouping（bundle 验证 + parity 冒烟）。"""
from __future__ import annotations

import threading

import pytest

import shared.position_manager as pm
from position_lifecycle import deps as lc_deps
from position_lifecycle import service as lc_mod


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0, 'sl': 1.5}
    pos.update(over)
    return pos


class TestConstructorBundles:
    def test_constructor_accepts_bundles(self):
        svc = pm._lifecycle_service()
        assert isinstance(svc.runtime, lc_deps.LifecycleRuntimeDeps)
        assert isinstance(svc.execution, lc_deps.LifecycleExecutionDeps)
        assert isinstance(svc.state, lc_deps.LifecycleStateDeps)
        assert isinstance(svc.protection, lc_deps.LifecycleProtectionDeps)
        assert isinstance(svc.action, lc_deps.LifecycleActionDeps)

    def test_factory_builds_fresh_bundles(self):
        svc1 = pm._lifecycle_service()
        svc2 = pm._lifecycle_service()
        assert svc1.runtime is not svc2.runtime
        assert svc1.execution is not svc2.execution
        assert svc1.state is not svc2.state
        assert svc1.protection is not svc2.protection
        assert svc1.action is not svc2.action

    def test_factory_builds_fresh_service(self):
        assert pm._lifecycle_service() is not pm._lifecycle_service()


class TestSeamIdentityThroughBundles:
    def test_exec_seam_identity(self, monkeypatch):
        watch = []
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: watch.append(1) or 'EXEC')
        svc = pm._lifecycle_service()
        assert svc.execution.exec_fn is not None

    def test_close_seam_identity(self, monkeypatch):
        def old_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc = pm._lifecycle_service()
        assert svc.action.close_fn is old_close

    def test_state_seam_identity(self, monkeypatch, fake_redis):
        svc = pm._lifecycle_service()
        assert svc.state.load is pm._load
        assert svc.state.save is pm._save
        assert svc.state.wcr is pm._was_closed_recently
        assert svc.state.mc is pm._mark_closed
        assert svc.state.clr is pm._clear_closed_marker
        assert svc.state.rq is pm._round_qty

    def test_protection_seam_identity(self, monkeypatch, fake_redis):
        svc = pm._lifecycle_service()
        assert svc.protection.wkr is pm._algo_start_worker
        assert svc.protection.enq is pm._algo_enqueue
        assert svc.protection.acx is pm._algo_cancel
        assert svc.protection.cxa is pm._cancel_all_algo


class TestLateBindingThroughBundles:
    def test_patch_before_factory(self, monkeypatch, fake_redis):
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        monkeypatch.setattr(pm, '_load', lambda: {'P': {'e': 1}})
        svc = pm._lifecycle_service()
        # bundle 当场捕获 patch 值
        assert svc.state.load() == {'P': {'e': 1}}

    def test_patch_between_factory_calls(self, monkeypatch, fake_redis):
        monkeypatch.setattr(pm, '_close', lambda *a, **k: 'OLD' or True)
        svc1 = pm._lifecycle_service()
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        svc2 = pm._lifecycle_service()
        assert svc1.action.close_fn is not svc2.action.close_fn

    def test_old_instance_retains_old_seam(self, monkeypatch, fake_redis):
        watch = []

        def old_close(*a, **kw):
            watch.append('old')
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc1 = pm._lifecycle_service()
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        assert svc1.action.close_fn is old_close


class TestHygiene:
    def test_no_import_time_bundle(self):
        src = open('position_lifecycle/deps.py').read()
        head = src.split('"""')[2] if len(src.split('"""')) > 2 else src
        assert 'DEFAULT_' not in head
        assert '__getattr__' not in head

    def test_no_service_singleton(self):
        before = set(threading.enumerate())
        import position_lifecycle
        assert before == set(threading.enumerate())
        src = open('position_lifecycle/service.py').read()
        assert '_SINGLETON' not in src

    def test_no_pm_reverse(self):
        for path in ('position_lifecycle/deps.py',
                     'position_lifecycle/service.py'):
            src = open(path).read()
            assert 'position_manager' not in src, path

    def test_import_no_thread_no_io(self):
        before = set(threading.enumerate())
        import position_lifecycle
        import position_lifecycle.deps
        assert before == set(threading.enumerate())
        deps_src = open(lc_deps.__file__).read()
        for token in ('requests', 'redis', 'threading'):
            assert token not in deps_src, token


class TestLifecycleParitySmoke:
    def test_open_success_through_bundles(self, fake_redis, monkeypatch):
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm, '_algo_enqueue', lambda *a: None)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        ok = pm.open_position('NEWG1', 'SHORT', 2.0, 10.0, 3, 1.9,
                              system='S6')
        assert ok is True

    def test_close_full_through_bundles(self, fake_redis, monkeypatch):
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm._exec_core, 'close_intent', staticmethod(
            lambda s, side, q: ('ci', s, side, q)))
        monkeypatch.setattr(pm, '_execution_service', lambda: type('X', (), {
            'execute_order': staticmethod(lambda i: type(
                'R', (), {'raw': {'orderId': 9,
                                  'status': 'FILLED',
                                  'executedQty': '10'}})())})())
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        pos = _pos()
        positions = {'AUSDT': pos}
        state = {}
        monkeypatch.setattr(pm, '_save', lambda p: state.update(p))
        monkeypatch.setattr(pm, '_load', lambda: dict(state))
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions, force=True)
        assert ok is True and 'AUSDT' not in state

    def test_partial_through_bundles(self, fake_redis, monkeypatch):
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(
                lambda s, side, q: ('pi', s, side, q)))
        monkeypatch.setattr(pm, '_execution_service', lambda: type(
            'X', (), {'execute_order': staticmethod(
                lambda i: type('R', (), {'raw': {'orderId': 9}})())})())
        monkeypatch.setattr(pm, '_save', lambda p: None)
        pos = _pos()
        pos['qty'] = 10.0
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, {'AUSDT': pos})
        assert pos['qty'] == 6.0                   # qty mutation 语义不变
