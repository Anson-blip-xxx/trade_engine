"""P8-01：静态常量迁移 parity（值逐字 / alias 同对象 / 导入干净）。"""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

import shared.position_manager as pm
from position_config import constants as pc


class TestValueParity:
    def test_system_cfg_values_frozen(self):
        assert len(pc.SYSTEM_CFG) == 5
        assert pc.SYSTEM_CFG['S8A']['be_done_threshold'] == 2.0
        assert pc.SYSTEM_CFG['S8A']['sl_breach_max'] == -5.0
        assert pc.SYSTEM_CFG['S8A']['partial_tp'] == {5: 0.3}
        assert pc.SYSTEM_CFG['S8A']['peak_guard'] == {
            'trigger_pct': 3.0, 'drawdown_pct': 2.0}
        assert pc.SYSTEM_CFG['S8B']['time_stop_min'] == 300
        assert pc.SYSTEM_CFG['S8B']['time_stop_fast_min'] == 150
        assert pc.SYSTEM_CFG['S8B']['funding_fast_threshold'] == -0.00015
        assert pc.SYSTEM_CFG['S6A']['partial_tp'] == {5: 0.5}
        assert pc.SYSTEM_CFG['S6B']['trail']['max_drawdown_pct'] == 20.0
        assert pc.SYSTEM_CFG['S6']['time_stop_min'] == 120

    def test_system_keys_literal(self):
        assert pc._SYSTEM_KEYS == {'S6': 'state:s6', 'S8': 'state:s8'}

    def test_lease_literals(self):
        assert pc._WS_LEASE_KEY == 'ws:leader'
        assert pc._WS_LEASE_TTL == 45

    def test_throttle_ints(self):
        assert pc._API_COOLDOWN == 3
        assert pc._ALGO_UPDATE_INTERVAL == 60
        assert pc._ALGO_MIN_CHANGE_PCT == 0.2


class TestLegacyAliases:
    def test_pm_alias_value_parity(self):
        assert pm.SYSTEM_CFG is pc.SYSTEM_CFG          # 同对象（identity）
        assert pm._SYSTEM_KEYS is pc._SYSTEM_KEYS
        assert pm._WS_LEASE_KEY == 'ws:leader' == pc._WS_LEASE_KEY
        assert pm._WS_LEASE_TTL == 45 == pc._WS_LEASE_TTL
        assert pm._API_COOLDOWN == 3 == pc._API_COOLDOWN
        assert pm._ALGO_UPDATE_INTERVAL == 60 == pc._ALGO_UPDATE_INTERVAL
        assert pm._ALGO_MIN_CHANGE_PCT == 0.2 == pc._ALGO_MIN_CHANGE_PCT

    def test_builtin_get_cfg_identity_preserved(self):
        """test_edge_cases 冻结 seam：`_get_cfg(...) is SYSTEM_CFG['S6A']`。"""
        from shared.position_manager import SYSTEM_CFG
        assert pm._get_cfg({'system': 'S6A'}) is SYSTEM_CFG['S6A']

    def test_factories_still_inject_same_literals(self):
        """legacy service factory 读到的参考值不变。"""
        svc = pm._reconcile_service()
        assert set(svc.sk) and svc.sk == {'S6': 'state:s6',
                                        'S8': 'state:s8'}
        mon = pm._monitoring_service()
        assert mon.lkey == 'ws:leader' and mon.lttl == 45


class TestCleanImport:
    def test_subprocess_import_no_side_effect(self):
        res = subprocess.run(
            [sys.executable, '-c',
             "import os; os.environ['PM_NO_WS']='1'; "
             "import position_config; import position_config.constants; "
             "print(position_config.constants.SYSTEM_CFG['S8A']["
             "'be_done_threshold'])"],
            capture_output=True, text=True, env=None)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip().startswith('2.0')

    def test_import_no_thread_spawn(self):
        before = set(threading.enumerate())
        import position_config
        import position_config.constants
        assert before == set(threading.enumerate())

    def test_no_runtime_backing_moved(self):
        """runtime mutable 的 8 组仍**只在 PM**（未复制到 config）。

        AST 校验 config 模块的顶层可写赋值目标（仅 APPROVED 常量）。"""
        import ast
        tree = ast.parse(open(pc.__file__).read())
        assigned = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
        approved = {'__name__', 'SYSTEM_CFG', '_SYSTEM_KEYS',
                    '_WS_LEASE_KEY', '_WS_LEASE_TTL', '_API_COOLDOWN',
                    '_ALGO_UPDATE_INTERVAL', '_ALGO_MIN_CHANGE_PCT',
                    '__annotations__'}
        assert assigned <= approved

        import shared.position_manager as pm_mod
        pm_src = open(pm_mod.__file__).read()
        for runtime_name in ('_WS_POSITIONS', '_WS_LOCK', '_ALGO_QUEUE',
                             '_ALGO_WORKER_STARTED', '_RECENTLY_GHOSTED',
                             '_CLOSE_ERROR_LOG_TS', '_last_api_call',
                             '_last_algo_update', '_S6_API'):
            assert runtime_name in pm_src, runtime_name
