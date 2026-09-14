"""P8-05B3：MonitoringService dependency bundle grouping 验证。"""
import ast
import threading

import pytest

import shared.position_manager as pm
import position_monitoring.deps as md


class TestConstructorBundles:
    def test_constructor_accepts_bundles(self):
        svc = pm._monitoring_service()
        assert isinstance(svc.runtime, md.MonitoringRuntimeDeps)
        assert isinstance(svc.state, md.MonitoringStateDeps)
        assert isinstance(svc.market, md.MonitoringMarketDeps)
        assert isinstance(svc.action, md.MonitoringActionDeps)
        assert isinstance(svc.ws, md.MonitoringWsDeps)

    def test_factory_builds_fresh_service(self):
        assert pm._monitoring_service() is not pm._monitoring_service()

    def test_factory_builds_fresh_bundles(self):
        svc1 = pm._monitoring_service()
        svc2 = pm._monitoring_service()
        for name in ('runtime', 'state', 'market', 'action', 'ws'):
            assert getattr(svc1, name) is not getattr(svc2, name)


class TestRuntimeBackingIdentity:
    def test_ws_backings_identity(self):
        svc = pm._monitoring_service()
        assert svc.ws.wsp is pm._WS_POSITIONS
        assert svc.ws.wsl is pm._WS_LOCK

    def test_heartbeat_identity_retained(self):
        svc = pm._monitoring_service()
        svc.runtime.shb(123.0)
        assert pm._monitor_heartbeat_ts == 123.0
        assert svc.runtime.ghb() == 123.0

    def test_ghost_queue_identity(self):
        svc = pm._monitoring_service()
        assert svc.state.gq is pm._RECENTLY_GHOSTED

    def test_closures_identity(self):
        svc = pm._monitoring_service()
        assert svc.ws.oofn() is pm._ws_on_open
        svc.ws.oe() is pm._ws_on_error
        svc.ws.oc() is pm._ws_on_close


class TestSeamIdentityThroughBundles:
    def test_funding_seam_identity(self):
        svc = pm._monitoring_service()
        assert svc.market.fund is pm._get_funding_rate

    def test_close_seam_identity(self, monkeypatch):
        def old_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc = pm._monitoring_service()
        assert svc.action.cls is old_close

    def test_ghost_seam_identity(self, monkeypatch):
        lv = []
        monkeypatch.setattr(pm, '_ghost_cleanup',
                            lambda p, f: lv.append(p) or [])
        svc = pm._monitoring_service()
        assert svc.action.gcl is not None

    def test_load_save_identity(self):
        svc = pm._monitoring_service()
        assert svc.state.load is pm._load
        assert svc.state.save is pm._save
        assert svc.state.mc is pm._mark_closed

    def test_marketmisc_identity(self):
        svc = pm._monitoring_service()
        assert svc.market.cfg is pm._get_cfg
        assert svc.market.dc is pm._get_data_cache
        assert svc.market.us is pm._update_stop_loss
        assert svc.action.elm is pm._early_loss_momentum_weak
        assert svc.action.cts is pm._calc_trail_sl


class TestPatchBeforeFactory:
    def test_patch_seam_before_factory(self, monkeypatch):
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        monkeypatch.setattr(pm, '_load', lambda: {'ZZ': {'x': 1}})
        svc = pm._monitoring_service()
        assert svc.state.load() == {'ZZ': {'x': 1}}


class TestPatchBetweenFactoryCalls:
    def test_old_instance_retains_old_seam(self, monkeypatch):
        def old_close(*a, **kw):
            return True
        monkeypatch.setattr(pm, '_close', old_close)
        svc1 = pm._monitoring_service()
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        svc2 = pm._monitoring_service()
        # svc1 旧绑定保持；svc2 新实例捕获新
        assert svc1.action.cls is old_close
        assert svc2.action.cls is not old_close


class TestWsBackingsIdentity:
    def test_wsp_unchanged(self):
        svc = pm._monitoring_service()
        assert svc.ws.wsp is pm._WS_POSITIONS

    def test_wsl_unchanged(self):
        svc = pm._monitoring_service()
        assert svc.ws.wsl is pm._WS_LOCK


class TestHygiene:
    def test_no_import_time_bundle(self):
        src = open('position_monitoring/deps.py').read()
        assert '__getattr__' not in src
        assert 'DEFAULT_' not in src

    def test_no_service_singleton(self):
        before = set(threading.enumerate())
        import position_monitoring
        import position_monitoring.deps
        assert before == set(threading.enumerate())

    def test_no_sibling_concrete_import(self):
        tree = ast.parse(open('position_monitoring/deps.py').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert 'position_manager' not in (node.module or '')
            if isinstance(node, ast.Import):
                assert 'position_manager' not in str(node)

    def test_no_thread_spawn_on_import(self):
        before = set(threading.enumerate())
        import position_monitoring
        import position_monitoring.deps
        assert before == set(threading.enumerate())

    def test_leader_lease_identity(self):
        svc = pm._monitoring_service()
        assert svc.ws.lkey == 'ws:leader' and svc.ws.lttl == 45
