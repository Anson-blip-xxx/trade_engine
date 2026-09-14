"""P7-08：Phase 7 架构收口 guard（依赖方向 / 单 owner / facade 完整性）。"""
import ast
import subprocess
import sys
import threading

import pytest

import shared.position_manager as pm
from position_state import service as ps_service
from position_ledger import service as pl_service
from position_protection import service as pp_service
from position_monitoring import service as mon_service
from position_reconcile import service as rc_service
from position_lifecycle import service as lc_service

SERVICES = {
    'position_state.service': ps_service,
    'position_ledger.service': pl_service,
    'position_protection.service': pp_service,
    'position_monitoring.service': mon_service,
    'position_reconcile.service': rc_service,
    'position_lifecycle.service': lc_service,
}
OWNERS = ('position_monitoring', 'position_reconcile',
          'position_lifecycle', 'position_state',
          'position_ledger', 'position_protection')


def _import_names(mod_obj, top_level_only=False):
    """AST 提取模块 import 语句的模块名集合。

    top_level_only=True 时只取模块体顶层（区分方法体内的惰性 import，
    那些是 frozen React seam，不算 import-time IO）。"""
    tree = ast.parse(open(mod_obj.__file__).read())
    nodes = tree.body if top_level_only else ast.walk(tree)
    names = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or '')
    return names


class TestCircularImport:
    def test_clean_subprocess_import_all(self):
        script = "\n".join(
            f"import {m}" for m in list(SERVICES) +
            ['shared.position_manager'])
        res = subprocess.run(
            [sys.executable, '-c',
             "import os; os.environ['PM_NO_WS']='1'; " + script],
            capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


class TestDependencyDirection:
    def test_no_service_imports_pm(self):
        for mod, mod_obj in SERVICES.items():
            for m in _import_names(mod_obj):
                assert 'position_manager' not in m, (mod, m)
                assert 'shared_executor' not in m, (mod, m)

    def test_no_sibling_concrete_import(self):
        for mod, mod_obj in SERVICES.items():
            this = mod.replace('.service', '')
            for m in _import_names(mod_obj):
                if m.startswith('position_'):
                    assert not any(other in m
                                   for other in OWNERS
                                   if other != this), (mod, m)

    def test_service_imports_are_io_free_top_level(self):
        """顶层 import 里无具体 redis/binance/requests/websocket 依赖。

        方法体内的惰性 import（如 `websocket` / `scripts.sandbox`）是
        frozen response seam，不计（AST 缝隙 = top_level_only）。"""
        for mod, mod_obj in SERVICES.items():
            for m in _import_names(mod_obj, top_level_only=True):
                assert not m.startswith(('redis_store', 'binance_api',
                                         'requests', 'websocket')), (mod, m)


class TestCleanImportSideEffects:
    def test_service_import_no_thread_spawn(self):
        before = set(threading.enumerate())
        import position_state
        import position_ledger
        import position_protection
        import position_monitoring
        import position_reconcile
        import position_lifecycle
        after = set(threading.enumerate())
        assert before == after

    def test_services_no_thread_import(self):
        """service 模块源码不含 threading.Thread 启动（准 runtime seam）。"""
        for mod, mod_obj in SERVICES.items():
            src = open(mod_obj.__file__).read()
            assert 'Thread(' not in src, mod


class TestRuntimeSingleOwner:
    def test_monitoring_backings_identity(self):
        svc = pm._monitoring_service()
        assert svc.gq is pm._RECENTLY_GHOSTED
        assert svc.wsp is pm._WS_POSITIONS
        assert svc.wsl is pm._WS_LOCK
        assert svc.lkey == pm._WS_LEASE_KEY == 'ws:leader'
        assert svc.lttl == pm._WS_LEASE_TTL == 45

    def test_reconcile_backing(self):
        svc = pm._reconcile_service()
        assert callable(svc.load) and callable(svc.save)
        assert callable(svc.wcr)

    def test_lifecycle_throttle_backing_module_global(self):
        svc = pm._lifecycle_service()
        assert isinstance(pm._CLOSE_ERROR_LOG_TS, dict)

    def test_algo_queue_single_backing(self):
        svc = pm._protection_service()
        assert svc is not None


class TestCompatibilityFacade:
    @pytest.mark.parametrize('name', [
        'open_position', 'close_position', '_close', '_partial_close',
        'monitor_all', '_monitor_one', 'reconcile_all', '_ghost_cleanup',
        '_ghost_cleanup_one', '_try_record_ghost_trade',
        '_notify_external_position', 'migrate_existing_positions',
        '_algo_enqueue', '_algo_cancel', '_cancel_all_algo',
        '_algo_start_worker', '_mark_closed', '_was_closed_recently',
        '_clear_closed_marker', '_load', '_save', '_load_meta',
        '_get_cfg', '_position_id', '_round_qty', 'log_position_summary',
    ])
    def test_legacy_api_exists(self, name):
        assert hasattr(pm, name), name

    def test_signatures_preserved(self):
        import inspect
        sig = inspect.signature(pm.open_position)
        assert list(sig.parameters)[:6] == ['symbol', 'side', 'entry',
                                            'qty', 'leverage', 'sl']
        assert sig.parameters['system'].default == ''
        assert sig.parameters['margin_type'].default == 'CROSSED'
        assert 'reason' in inspect.signature(pm.close_position).parameters
        assert 'force' in inspect.signature(pm._close).parameters

    def test_state_port_reused(self):
        src = open(pm.__file__).read()
        assert '_exec_pos_state.RedisPositionStateAdapter' in src


class TestPortReuse:
    def test_protection_port_reused(self):
        src = open(pp_service.__file__).read()
        assert 'ProtectionPort' in src or 'enqueue' in src
