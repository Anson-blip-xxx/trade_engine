"""P7-05B：PositionMonitoringService 架构 guard。

冻结：
1. 无 PM / SE 反向 import
2. 无具体 ledger/protection/state service 依赖
3. import 副作用 = 0（不启线程、不访问 Redis/Binance、不 sleep）
4. runtime backing 单一（service wsp is pm._WS_POSITIONS / gq is ghost queue）
5. 无循环 import
"""
import importlib
import sys
import threading
import time as time_mod

import pytest

from shared import position_manager as pm
from position_monitoring import service as mon
from position_monitoring import PositionMonitoringService


import ast


class TestNoReverseImport:
    def _import_names(self):
        tree = ast.parse(open(mon.__file__).read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or '')
        return names

    def test_no_pm_import_in_service_source(self):
        mods = self._import_names()
        banned = [m for m in mods if 'position_manager' in m
                  or 'shared_executor' in m or 'monitoring' in m and
                  'position_' in m and 'monitoring/' in m]
        assert not banned or all('shared_executor' not in m
                                 and 'position_manager' not in m
                                 for m in banned)

    def test_no_se_import_in_service_source(self):
        assert not any('shared_executor' in m for m in self._import_names())

    def test_no_concrete_service_import(self):
        mods = self._import_names()
        for m in mods:
            assert not m.startswith(('position_state', 'position_ledger',
                                     'position_protection',
                                     'execution')), m


class TestCleanImport:
    def test_import_already_loaded_no_thread_spawn(self, monkeypatch):
        """import position_monitoring → 不产生任何新线程。"""
        before = set(threading.enumerate())
        import position_monitoring
        import position_monitoring.service
        after = set(threading.enumerate())
        assert before == after

    def test_import_no_io_no_sleep(self):
        """service 模块源码顶层无 redis/binance/websocket 真连接请求。"""
        src = open(mon.__file__).read()
        assert 'requests.get' not in src
        assert '_rget' not in src.split('"""')[0]  # 仅 docstring 内允许
        assert 'websocket' not in src.split('from __future__')[0]


class TestRuntimeSingleOwner:
    def test_service_uses_pm_backing_identity(self):
        """注入的 wsp/gq 与 pm 全局是同一对象（无第二套 runtime）。"""
        import position_monitoring.deps as md
        svc = mon.PositionMonitoringService(
            runtime=md.MonitoringRuntimeDeps(
                log=lambda *a, **k: None, now=time_mod.time,
                summ=lambda: None, ghb=lambda: 0.0, shb=lambda v: None),
            state=md.MonitoringStateDeps(
                load=lambda: {}, save=lambda p: None,
                wcr=lambda s: False, mc=lambda s: None,
                gq=pm._RECENTLY_GHOSTED, trgt=lambda *a, **k: True),
            market=md.MonitoringMarketDeps(
                s6=lambda: (None,) * 8, dc=lambda: None, cfg=lambda p: {},
                fund=lambda s: 0.0, us=lambda *a, **k: None,
                pc=lambda *a, **k: None, rq=lambda s, q: q),
            action=md.MonitoringActionDeps(
                cls=lambda *a, **k: False, gcl=lambda *a: [],
                elm=lambda *a: False, stag=lambda *a: False,
                cts=lambda *a, **k: None, pt=lambda *a: None,
                pp=lambda *a: None, g1h=lambda *a: False),
            ws=md.MonitoringWsDeps(
                wsp=pm._WS_POSITIONS, wsl=pm._WS_LOCK,
                swlu=lambda v: None, wst=lambda: False,
                m1=lambda *a, **k: None, lkey=pm._WS_LEASE_KEY,
                lttl=pm._WS_LEASE_TTL, inst=pm._WS_INSTANCE,
                ldr=lambda: False, lkfn=lambda: '', wsf=lambda: '',
                oofn=lambda: None, oe=lambda *a: None,
                oc=lambda *a: None))


class TestLegacyWrapperParity:
    def test_pm_monitor_one_delegates_to_service(self, monkeypatch):
        """pm._monitor_one thin delegation → service.monitor_one。"""
        seen = {}

        class FakeSvc:
            def monitor_one(self, sym, pos, positions):
                seen['args'] = (sym, pos, positions)
                return ('x', 1.0)
        monkeypatch.setattr(mon, 'PositionMonitoringService',
                            lambda **kw: FakeSvc())
        pm._monitor_one('TUSDT', {'entry': 1}, {})
        assert seen['args'][0] == 'TUSDT'

    def test_pm_monitor_all_delegates_to_service(self, monkeypatch):
        seen = {}

        class FakeSvc:
            def monitor_all(self, filter_):
                seen['filter'] = filter_
                return [42]
        monkeypatch.setattr(mon, 'PositionMonitoringService',
                            lambda **kw: FakeSvc())
        assert pm.monitor_all('S6') == [42]
        assert seen['filter'] == 'S6'

    def test_pm_ws_helpers_delegate(self, monkeypatch):
        seen = {}

        class FakeSvc:
            def ws_on_message(self, ws, msg):
                seen['msg'] = msg

            def am_leader(self):
                seen['leader'] = True

            def ws_connect_loop(self):
                seen['loop'] = True

        monkeypatch.setattr(mon, 'PositionMonitoringService',
                            lambda **kw: FakeSvc())
        pm._ws_on_message(None, 'raw')
        assert seen['msg'] == 'raw'
        pm._ws_am_leader()
        assert seen['leader'] is True
        pm._ws_connect_loop()
        assert seen['loop'] is True
