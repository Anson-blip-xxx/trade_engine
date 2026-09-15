"""P8-06A：runtime ownership golden（queue / WS / heartbeat / 状态背书）。"""
from __future__ import annotations

import ast
import threading

import pytest

import shared.position_manager as pm
import position_monitoring.deps as md

class TestAlgoQueueOwner:
    def test_queue_owner_pm(self):
        assert hasattr(pm, '_ALGO_QUEUE')
        assert isinstance(pm._ALGO_QUEUE, list)
        assert hasattr(pm, '_ALGO_QUEUE_LOCK')
        assert hasattr(pm, '_ALGO_WORKER_STARTED')

    def test_protection_service_queue_backing_identity(self):
        svc = pm._protection_service()
        assert svc._enqueue_fn is pm._algo_enqueue

    def test_lifecycle_protection_bundle_backing_identity(self):
        svc = pm._lifecycle_service()
        assert svc.protection.enq is pm._algo_enqueue
        assert svc.protection.wkr is pm._algo_start_worker

    def test_no_shadow_queue(self):
        assert not hasattr(pm._lifecycle_service(), '_ALGO_QUEUE')

class TestWsBackingOwner:
    def test_ws_positions_owner_pm(self):
        svc = pm._monitoring_service()
        assert svc.ws.wsp is pm._WS_POSITIONS
        assert svc.ws.wsl is pm._WS_LOCK

    def test_no_second_ws_backing(self):
        svc = pm._monitoring_service()
        assert not hasattr(svc.runtime, 'wsp')
        assert not hasattr(svc.market, 'wsp')

class TestHeartbeatBacking:
    def test_shb_ghb_backing_identity(self):
        svc = pm._monitoring_service()
        svc.runtime.shb(77.0)
        assert pm._monitor_heartbeat_ts == 77.0
        assert svc.runtime.ghb() == 77.0
        svc.runtime.shb(0.0)

    def test_heartbeat_pm_global_backing(self):
        assert isinstance(pm._monitor_heartbeat_ts, float)

class TestRecentlyGhosted:
    def test_dead_runtime_pm_owner(self):
        svc = pm._monitoring_service()
        assert svc.state.gq is pm._RECENTLY_GHOSTED
        pm._RECENTLY_GHOSTED.append(('X', 'x', 1, 2, 3, 'LONG'))
        pm._RECENTLY_GHOSTED.clear()             # producer dead——保持
    def test_no_producer(self, monkeypatch):
        assert pm._RECENTLY_GHOSTED == []        # dead runtime 默认

class TestPosCacheDeferred:
    def test_pos_cache_still_owned_by_se(self):
        import strategies.shared_executor as se
        assert hasattr(se, '_POS_CACHE')
        assert not hasattr(pm, '_POS_CACHE')

class TestRestartSemantics:
    def test_process_local_reset_model(self):
        """模型回落：重启后 algo queue/flag/WS/freshly backed heartbeat /
        recent ghosted/`_WS_INSTANCE` 都在 finish → same process-local。"""
        thr = threading.enumerate().__len__()
        assert isinstance(thr, int)
        # Dark-semanthics：仅对当前进程 fresh path restart 前后意义重大——
        # script 级 probe（快照 + restart）端到端
        import subprocess
        res = subprocess.run(
            ['python3', '-c',
             "import os; os.environ['PM_NO_WS']='1';"
             "import shared.position_manager as pm;"
             "assert pm._ALGO_QUEUE == [] and pm._ALGO_WORKER_STARTED is False;"
             "assert pm._WS_POSITIONS == {} and pm._WS_LAST_UPDATE == 0.0;"
             "assert pm._RECENTLY_GHOSTED == [];"
             "assert pm._monitor_heartbeat_ts == 0.0;"
             "import uuid;"
             "assert '-' in pm._WS_INSTANCE;"
             "print('ok')"],
            capture_output=True, text=True)
        assert res.stdout.strip().endswith('ok'), res.stderr

class TestLeaderState:
    def test_leader_instance_process_scoped(self):
        svc = pm._monitoring_service()
        assert pm._WS_LEASE_KEY == 'ws:leader'
        assert pm._WS_LEASE_TTL == 45
        assert '-' in pm._WS_INSTANCE   # pid-hex8
        assert svc.ws.lkey == pm._WS_LEASE_KEY

class TestNoShadowBacking:
    import pytest
    def test_no_double_backing_anywhere(self):
        paths = ('shared/position_manager.py',
                 'position_lifecycle/', 'position_reconcile/',
                 'position_monitoring/')
        src_files = [
            'shared/position_manager.py',
            'position_lifecycle/deps.py', 'position_lifecycle/service.py',
            'position_reconcile/deps.py', 'position_reconcile/service.py',
            'position_monitoring/deps.py', 'position_monitoring/service.py'
        ]
        ALLOWED_OWNER = 'shared/position_manager.py'
        booting = {'_ALGO_QUEUE', '_ALGO_WORKER_STARTED',
                   '_WS_POSITIONS', '_WS_LOCK', '_RECENTLY_GHOSTED'}
        for path in src_files:
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            if path == ALLOWED_OWNER:
                                continue  # PM 是单 owner
                            assert t.id not in booting, (path, t.id)


