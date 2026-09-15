"""P8-05C：Constructor Grouping Integration Closure。"""
import ast
import threading

import pytest

import shared.position_manager as pm
import position_lifecycle.deps as lc_deps
import position_reconcile.deps as rc_deps
import position_monitoring.deps as mon_deps


class TestFinalConstructorShape:
    @pytest.mark.parametrize('bundle_kw, deps_mod', [
        ('runtime', lc_deps), ('execution', lc_deps), ('state', lc_deps),
        ('protection', lc_deps), ('action', lc_deps),
    ])
    def test_lifecycle_5_bundles(self, bundle_kw, deps_mod):
        svc = pm._lifecycle_service()
        assert hasattr(svc, bundle_kw)

    def test_final_constr_shape(self):
        svc1 = pm._lifecycle_service()
        svc2 = pm._lifecycle_service()
        # 每次调用新实例
        assert svc1 is not svc2
        # bundle fresh
        assert svc1.runtime is not svc2.runtime
        assert svc1.execution is not svc2.execution
        assert svc1.state is not svc2.state
        assert svc1.protection is not svc2.protection
        assert svc1.action is not svc2.action

    def test_monitoring_runtime_identity(self):
        svc1 = pm._monitoring_service()
        svc2 = pm._monitoring_service()
        assert svc1 is not svc2
        for n in ('runtime', 'state', 'market', 'action', 'ws'):
            assert getattr(svc1, n) is not getattr(svc2, n)

    def test_reconcile_runtime_identity(self):
        svc1 = pm._reconcile_service()
        svc2 = pm._reconcile_service()
        assert svc1 is not svc2
        for n in ('runtime', 'state', 'coordination', 'notification',
                  'action'):
            assert getattr(svc1, n) is not getattr(svc2, n)


class TestLateBindingAcrossServices:
    def test_lifecycle_patch_between(self, monkeypatch):
        def old_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc1 = pm._lifecycle_service()
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)

        def new_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', new_close)
        svc2 = pm._lifecycle_service()
        assert svc1.action.close_fn is old_close
        assert svc2.action.close_fn is new_close

    def test_reconcile_patch_between(self, monkeypatch):
        monkeypatch.setattr(pm, '_lock_acquire', lambda k, o, ttl=45: True)
        svc1 = pm._reconcile_service()
        monkeypatch.setattr(pm, '_lock_acquire', lambda *a, **kw: False)
        svc2 = pm._reconcile_service()
        assert svc1.coordination.lacq is not svc2.coordination.lacq

    def test_monitoring_fully_frozen_time(self, monkeypatch):
        def old_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc1 = pm._monitoring_service()
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        svc2 = pm._monitoring_service()
        assert svc1.action.cls is old_close
        assert svc2.action.cls is not old_close


class TestRuntimeIdentityMonitoring:
    def test_monitoring_ws_runtime_identity(self):
        svc1 = pm._monitoring_service()
        svc2 = pm._monitoring_service()
        assert svc1.ws.wsp is pm._WS_POSITIONS
        assert svc1.ws.wsl is pm._WS_LOCK
        assert svc2.ws.wsp is pm._WS_POSITIONS
        assert svc1.ws.wsp is svc2.ws.wsp          # 同一 backing

    def test_monitoring_ghost_backing_identity(self):
        svc = pm._monitoring_service()
        assert svc.state.gq is pm._RECENTLY_GHOSTED

    def test_reconcile_no_ghost_queueownership(self):
        svc = pm._reconcile_service()
        for bundle in (svc.runtime, svc.state, svc.coordination,
                       svc.notification, svc.action):
            assert not hasattr(bundle, 'gq')

    def test_lifecycle_no_monitoring_runtime(self):
        svc = pm._lifecycle_service()
        for bundle in (svc.runtime, svc.execution, svc.state,
                       svc.protection, svc.action):
            assert not hasattr(bundle, 'wsp')

    def test_monitoring_heartbeat_backing_identity(self):
        svc = pm._monitoring_service()
        svc.runtime.shb(99.0)
        assert pm._monitor_heartbeat_ts == 99.0
        assert svc.runtime.ghb() == 99.0


class TestHygieneNoImportTimeNoDI:
    def test_no_import_time_bundles(self):
        paths = ('position_lifecycle/deps.py',
                 'position_reconcile/deps.py',
                 'position_monitoring/deps.py',
                 'shared/position_manager.py')
        for path in paths:
            src = open(path).read()
            for token in ('DEFAULT_RUNTIME_DEPS', 'DEFAULT_STATE_DEPS',
                          'DEFAULT_LIFECYCLE_DEPS', 'DEFAULT_RECONCILE_DEPS',
                          'DEFAULT_MONITORING_DEPS'):
                assert token not in src, (path, token)

    def test_no_cached_service(self):
        src = open('shared/position_manager.py').read()
        assert '_CACHED_LIFECYCLE' not in src
        assert '_CACHED_RECONCILE' not in src
        assert '_CACHED_MONITORING' not in src

    def test_no_di_framework(self):
        for path in ('shared/position_manager.py',
                     'position_lifecycle/deps.py',
                     'position_reconcile/deps.py',
                     'position_monitoring/deps.py',
                     'position_lifecycle/service.py',
                     'position_reconcile/service.py',
                     'position_monitoring/service.py'):
            src = open(path).read()
            for token in ('Container', 'Injector', 'ServiceLocator', 'Registry'):
                assert token not in src, (path, token)

    def test_no_dynamic_getattr_lookup(self):
        paths = ('position_lifecycle/deps.py',
                 'position_reconcile/deps.py',
                 'position_monitoring/deps.py')
        for path in paths:
            src = open(path).read()
            assert '__getattr__' not in src, path

    def test_no_reverse_import(self):
        paths = ('position_lifecycle/deps.py',
                 'position_reconcile/deps.py',
                 'position_monitoring/deps.py',
                 'position_lifecycle/service.py',
                 'position_reconcile/service.py',
                 'position_monitoring/service.py')
        for path in paths:
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert 'shared.position_manager' not in str(node), path
                elif isinstance(node, ast.ImportFrom):
                    assert 'shared.position_manager' not in (node.module or ''), path


class TestCompatibilityProperties:
    def test_monitoring_props_bundle_backed_not_dynamic(self, monkeypatch):
        svc = pm._monitoring_service()
        w1 = svc.ws.wsp
        assert w1 is pm._WS_POSITIONS
        w2 = svc.ws.wsp
        assert w1 is w2                          # bundle 内 captured
        # 对 patch 顺序成功：
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        svc = pm._monitoring_service()
        assert svc.action.cls is not None

    def test_lifecycle_props(self):
        svc = pm._lifecycle_service()
        assert svc.state.load is pm._load
        assert svc.action.close_fn is not None

    def test_reconcile_props(self):
        svc = pm._reconcile_service()
        assert svc.state.load is pm._load
        assert svc.notification.pg is pm._pg_record_event


class TestFrozenDataclasses:
    def test_bundle_dataclasses_frozen(self):
        for cls in (lc_deps.LifecycleRuntimeDeps,
                    lc_deps.LifecycleExecutionDeps,
                    lc_deps.LifecycleStateDeps,
                    lc_deps.LifecycleProtectionDeps,
                    lc_deps.LifecycleActionDeps,
                    rc_deps.ReconcileRuntimeDeps,
                    rc_deps.ReconcileStateDeps,
                    rc_deps.ReconcileCoordinationDeps,
                    rc_deps.ReconcileNotificationDeps,
                    rc_deps.ReconcileActionDeps,
                    mon_deps.MonitoringRuntimeDeps,
                    mon_deps.MonitoringStateDeps,
                    mon_deps.MonitoringMarketDeps,
                    mon_deps.MonitoringActionDeps,
                    mon_deps.MonitoringWsDeps):
            import dataclasses
            assert dataclasses.is_dataclass(cls)
            assert cls.__dataclass_params__.frozen is True

    def test_frozen_blocks_reassign(self):
        b = lc_deps.LifecycleRuntimeDeps(log=None, now=None)
        with pytest.raises(Exception):
            b.log = 'patched'
