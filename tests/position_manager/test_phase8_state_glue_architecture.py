"""P8-02：state glue 收口架构 guard（_load thin / 门面/依赖/双写不变）。"""
import ast
import subprocess
import sys
import threading

import pytest

import shared.position_manager as pm
import position_state.service as ps


class TestLoadThinFacade:
    def test_load_delegates_service_once(self):
        src = open(pm.__file__).read()
        start = src.index('def _load()')
        body = src[start:start + 600]
        assert '_state_service().load_assembly' in body
        # closure 内联不再出现于 _load 域
        assert 'meta = _load_meta()' not in body

    def test_glue_extracted_as_module_funcs(self):
        assert callable(pm._ws_snapshot)
        assert callable(pm._merge_and_save)
        assert callable(pm._meta_filtered)

    def test_load_wrapper_parity_chain(self, fake_redis, monkeypatch):
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
        monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
        monkeypatch.setattr(pm, '_ws_snapshot',
                            lambda: (0, {}))          # 模拟 WS 空
        pm._save({'AUSDT': {'entry': 1.0, 'qty': 1.0, 'side': 'LONG'}})
        loaded = pm._load()
        assert 'AUSDT' in loaded                       # meta 兜底可达


class TestParsePositionRiskMoved:
    def test_pure_from_pm(self):
        """REST 解析机械已迁 StateService，PM 只持 fetch/吞错。"""
        svc_src = open(sys.modules['position_state.service'].__file__).read()
        assert 'def parse_position_risk' in svc_src
        pm_src = open(pm.__file__).read()
        # PM 侧仍持有 fetch + 吞错 + 调 helper
        assert '_ext_pos_parse(' in pm_src

    def test_no_second_rest_parse_impl(self):
        """无第二份解析复制品（增量 out-dict 唯一实现点）。"""
        svc_src = open(sys.modules['position_state.service'].__file__).read()
        assert svc_src.count('for p in raw_r') == 1


class TestStateServiceNoReverse:
    def _top_imports(self, mod_obj):
        tree = ast.parse(open(mod_obj.__file__).read())
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or '')
        return mods

    def test_no_banned_imports(self, monkeypatch):
        banned = ('position_manager', 'shared_executor',
                  'position_monitoring', 'position_reconcile',
                  'position_lifecycle', 'position_ledger',
                  'position_protection')
        for m in self._top_imports(sys.modules['position_state.service']):
            assert not any(b in m for b in banned), m


class TestRuntimeOwnerUnchanged:
    def test_ws_owners_stay_in_pm(self):
        for name in ('_WS_POSITIONS', '_WS_LOCK', '_WS_LAST_UPDATE',
                     '_RECENTLY_GHOSTED',
                     '_ALGO_QUEUE', '_ALGO_WORKER_STARTED'):
            assert hasattr(pm, name), name

    def test_pos_cache_in_se_unchanged(self):
        import strategies.shared_executor as se
        src = open(se.__file__).read()
        assert '_POS_CACHE' in src          # dual writer 不动（未迁）


class TestCleanImport:
    def test_subprocess_import_all_clean(self):
        res = subprocess.run(
            [sys.executable, '-c',
             "import os; os.environ['PM_NO_WS']='1'; "
             "import position_state.service; "
             "import shared.position_manager; print('ok')"],
            capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_import_no_thread_spawn(self):
        before = set(threading.enumerate())
        import position_state.service
        assert before == set(threading.enumerate())
