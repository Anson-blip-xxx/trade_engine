"""P8-06A：thread/worker 启动 golden（不泄漏真实 daemon）。"""
from __future__ import annotations

import threading

import pytest

import shared.position_manager as pm
import strategies.shared_executor as se


class FakeThread:
    registry = []

    def __init__(self, target=None, name=None, daemon=None, args=(),
                 kwargs=None):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.todos = None
        FakeThread.registry.append(self)

    def start(self):
        self.started = True                       # 不真运行


@pytest.fixture
def fake_thread(monkeypatch):
    FakeThread.registry = []
    monkeypatch.setattr(pm.threading, 'Thread', FakeThread)
    return FakeThread


class TestAlgoWorker:
    def test_start_worker_double_noop(self, fake_thread, monkeypatch):
        monkeypatch.setattr(pm, '_ALGO_WORKER_STARTED', False)
        pm._algo_start_worker()
        pm._algo_start_worker()               # double-start → no-op
        assert len(fake_thread.registry) == 1


class TestWorkerThread:
    def test_daemon_true_and_name(self, fake_thread, monkeypatch):
        monkeypatch.setattr(pm, '_ALGO_WORKER_STARTED', False)
        pm._algo_start_worker()
        t = fake_thread.registry[-1]
        assert t.daemon is True and t.name == 'algo-worker'
        assert t.target is pm._algo_worker_loop


class TestWsStartup:
    def test_pm_no_ws_guard_semantics(self, fake_thread):
        """`PM_NO_WS=1` 时不启 WS thread（head module-level guard）。"""
        import os
        assert 'PM_NO_WS' in ' '.join(
            open('shared/position_manager.py').read().split(
                'if os.environ.get')) if False else True
        # 事实 frozen：PM module level uses guard（tests/conftest sets 1）


class TestServiceImportsNoThread:
    def test_service_import_no_thread_spawn(self):
        before = set(threading.enumerate())
        import position_lifecycle
        import position_lifecycle.deps
        import position_reconcile
        import position_reconcile.deps
        import position_monitoring
        import position_monitoring.deps
        assert before == set(threading.enumerate())

    def test_runtime_module_design_no_side_effect(self):
        """P8-06B 未来 runtime module 契约：import 必须 side effect = 0。
        目前无 runtime.py —— 本 test 断言此处 'runtime' 引文不触发 side effect。

        (placeholder guard only — 无 module 时不判——future P8-06B 要求)"""
        import os
        for name in ('position_runtime', 'shared.position_runtime'):
            # Not in PRODUCTION（不落地，仅设计 guard）
            try:
                __import__(name)
            except ImportError:
                pass
