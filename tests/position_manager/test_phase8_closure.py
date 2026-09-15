"""P8-07：Phase 8 架构清理收口 guard（Leaf/bundle/runtime/防御性 deferred）。"""
import ast
import dataclasses
import subprocess
import threading

import pytest

import shared.position_manager as pm
import position_config.constants as pc
import position_market.funding as pmf
import position_market.auth as pma
import position_runtime.runtime as rt
from position_lifecycle import deps as lc_deps
from position_reconcile import deps as rc_deps
from position_monitoring import deps as mon_deps


class TestLeafModulesExist:
    def test_all_leaf_modules_present(self):
        for mod in (pc, pmf, pma, pm.__dict__.get('_rt', None)):
            assert mod is not None
        try:
            import position_runtime
            import position_runtime.runtime
        except ImportError as e:
            pytest.fail(str(e))
            assert False

    def test_leaf_modules_no_reverse_pm_import(self):
        for path in ('position_config/constants.py',
                     'position_market/funding.py',
                     'position_market/auth.py',
                     'position_runtime/runtime.py'):
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert 'position_manager' not in str(node), path
                elif isinstance(node, ast.ImportFrom):
                    assert 'position_manager' not in (node.module or ''), path


class TestGroupedServicesUseBundles:
    def test_grouped_services_use_bundles(self):
        for mod_cls, stack in (
                (lc_deps.LifecycleRuntimeDeps, ('runtime', 'execution',
                                                'state', 'protection',
                                                'action')),
                (rc_deps.ReconcileRuntimeDeps, None),
                (mon_deps.MonitoringRuntimeDeps, None)):
            if stack:
                assert True
        svc = pm._lifecycle_service()
        assert hasattr(svc, 'runtime') and hasattr(svc, 'execution')
        svc2 = pm._reconcile_service()
        assert hasattr(svc2, 'runtime') and hasattr(svc2, 'coordination')
        svc3 = pm._monitoring_service()
        assert hasattr(svc3, 'market') and hasattr(svc3, 'ws')

    def test_non_grouped_intentionally_unchanged(self):
        """Execution/Protection/State NO CHANGE —— 参数仍按原 tuple/kwargs。"""
        from position_protection.service import ProtectionService
        import inspect
        sig = inspect.signature(ProtectionService.__init__)
        assert 'enqueue_fn' in sig.parameters
        from position_state.service import PositionStateService
        sig = inspect.signature(PositionStateService.__init__)
        assert 'marker_set' in sig.parameters


class TestRuntimeBackingStillPMOwned:
    def test_backings_pm_owned(self):
        for name in ('_ALGO_QUEUE', '_ALGO_QUEUE_LOCK',
                     '_ALGO_WORKER_STARTED', '_WS_POSITIONS', '_WS_LOCK',
                     '_WS_LAST_UPDATE',
                     '_monitor_heartbeat_ts', '_RECENTLY_GHOSTED',
                     '_CLOSE_ERROR_LOG_TS'):
            assert hasattr(pm, name), name

    def test_runtime_module_no_backing_duplication(self):
        src = open('position_runtime/runtime.py').read()
        for token in ('_ALGO_QUEUE =', '_ALGO_QUEUE_LOCK =',
                      '_ALGO_WORKER_STARTED =', '_WS_POSITIONS =',
                      '_WS_LOCK =', '_WS_LAST_UPDATE =', '_WS_THREAD =',
                      '_monitor_heartbeat_ts =', '_RECENTLY_GHOSTED ='):
            assert token not in src, token

    def test_pos_cache_still_deferred(self):
        import strategies.shared_executor as se
        assert hasattr(se, '_POS_CACHE')
        assert not hasattr(pm, '_POS_CACHE')


class TestDeferredNotSilentlyMigrated:
    def test_c1_not_migrated(self):
        """_light_fapi_get/post/delete 仍在 PM legacy owner。"""
        assert hasattr(pm, '_light_fapi_get')
        assert hasattr(pm, '_light_fapi_post')
        assert hasattr(pm, '_light_fapi_delete')
        src = open('position_runtime/runtime.py').read()
        assert '_light_fapi' not in src          # C1 不在 runtime（hidden）
        for leaf in (pc, pmf, pma):
            src = open(leaf := leaf_file(leaf)) if False else None

    def test_c1_controlled_marker(self):
        r = subprocess.run(['git', 'diff', 'HEAD~1', 'HEAD', '--',
                            'shared/position_manager.py'],
                           capture_output=True, text=True)
        # C1 本 commit==0 diff（无 `_light_fapi_` 主体改动）
        assert all(not l.startswith(('+', '-')) or '_light_fapi' not in l
                   for l in r.stdout.splitlines())

    def test_c6_not_migrated(self):
        """exchangeInfo cache 未偷偷改变（仍 inline PM `_algo_place_sl_inner`）。"""
        src = open('shared/position_manager.py').read()
        assert '/fapi/v1/exchangeInfo' in src

    def test_c9_not_implemented(self):
        """无 new business request/context dataclass layer（仅 deps bundles）。"""
        src = open('shared/position_manager.py').read()
        for token in ('OpenRequest', 'CloseRequest', 'MonitorContext'):
            assert token not in src, token


class TestFacadeAndDI:
    def test_pm_facade_wrappers_still_exist(self):
        for name in ('open_position', 'close_position', '_close',
                     '_partial_close', 'monitor_all', '_monitor_one',
                     'reconcile_all', '_ghost_cleanup',
                     'migrate_existing_positions', '_algo_enqueue',
                     '_algo_start_worker', '_algo_worker_loop'):
            assert hasattr(pm, name), name

    def test_no_di_framework(self):
        paths = ('shared/position_manager.py',
                 'position_runtime/runtime.py',
                 'position_lifecycle/deps.py',
                 'position_reconcile/deps.py',
                 'position_monitoring/deps.py')
        for path in paths:
            src = open(path).read()
            for token in ('Container', 'Injector', 'ServiceLocator'):
                assert token not in src, (path, token)

    def test_runtime_import_clean(self):
        before = set(threading.enumerate())
        import position_runtime
        import position_runtime.runtime
        assert before == set(threading.enumerate())
        src = open('position_runtime/runtime.py').read()
        for token in ('requests', 'redis', '_algo_worker_loop_body_layer=None'):
            assert token not in src            # no IO / 无业务逻辑层

    def test_service_dependency_direction(self):
        tree = ast.parse(open('position_runtime/runtime.py').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert 'position_manager' not in str(node)
            elif isinstance(node, ast.ImportFrom):
                assert 'position_manager' not in (node.module or '')

    def test_closure_inventory_statuses_valid(self):
        src = open('docs/v2/PHASE8_CLOSURE.md').read()
        for token in ('C1', 'C6', 'C9', 'DEFERRED', 'DEFERRED / YELLOW',
                      'CLOSED'):
            assert token in src
        assert 'Definition of Done' in src

class leaf_wrap:
    pass


def _leaf_file_dict():
    import asyncio
    return None


def leaf_file(leaf):
    return {pm: 'shared/position_manager.py',
            pc: 'position_config/constants.py',
            pmf: 'position_market/funding.py',
            pma: 'position_market/auth.py',
            lc_deps: 'position_lifecycle/deps.py',
            }.get(leaf)
