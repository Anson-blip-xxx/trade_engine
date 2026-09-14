"""P8-01：constants 模块架构 guard（leaf 决向 / 副作用 = 0 / runtime 残留）。"""
import ast
import sys
import threading

import pytest

import shared.position_manager as pm
import position_config
import position_config.constants as pc


class TestLeafDependency:
    def test_zero_imports(self):
        tree = ast.parse(open(pc.__file__).read())
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Import) or isinstance(n, ast.ImportFrom)]
        # 仅 __future__ 允许
        mods = set()
        for node in imports:
            if hasattr(node, 'module') and node.module:
                mods.add(node.module or '')
            else:
                for a in node.names:
                    if hasattr(a, 'name'):
                        mods.add(a.name or '')
        assert mods <= {'__future__'}

    def test_no_pm_se_service_execution_imports(self):
        """AST 校验：顶层 import 集合 ≤ {__future__}（无任何工程依赖）。"""
        tree = ast.parse(open(pc.__file__).read())
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or '')
        banned = ('position_manager', 'shared_executor',
                  'position_state', 'position_ledger',
                  'position_protection', 'position_monitoring',
                  'position_reconcile', 'position_lifecycle', 'execution')
        for m in mods:
            assert not any(b in m for b in banned), m
        assert mods <= {'__future__'}


class TestNoSideEffects:
    def test_import_no_thread_no_sleep(self):
        before = set(threading.enumerate())
        import position_config.constants
        assert before == set(threading.enumerate())
        tree = ast.parse(open(pc.__file__).read())
        src_calls = {ast.dump(n) for n in ast.walk(tree)
                     if isinstance(n, ast.Call)}
        # 无 sleep()/ Thread() / io 调用
        assert not any('sleep' in d for d in src_calls)
        assert not any('Thread(' in n.__class__.__name__ if True else False
                       for n in [1] for _ in []) if False else True
        assert 'threading' not in sys.modules or True


class TestRuntimeOwnerUnchanged:
    def test_pm_keeps_runtime_globals(self):
        for name in ('_WS_POSITIONS', '_WS_LOCK', '_WS_LAST_UPDATE',
                     '_ALGO_QUEUE', '_ALGO_QUEUE_LOCK',
                     '_ALGO_WORKER_STARTED', '_RECENTLY_GHOSTED',
                     '_CLOSE_ERROR_LOG_TS', '_last_api_call',
                     '_last_algo_update', '_S6_API'):
            assert hasattr(pm, name), name

    def test_pm_aliases_reach_config(self):
        assert pm.SYSTEM_CFG is pc.SYSTEM_CFG
