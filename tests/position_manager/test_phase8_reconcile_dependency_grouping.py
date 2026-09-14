"""P8-05B2：ReconcileService dependency bundle grouping 验证。"""
import ast
import threading

import pytest

import shared.position_manager as pm
from position_reconcile import deps as rc_deps


class TestConstructorBundles:
    def test_constructor_accepts_bundles(self):
        svc = pm._reconcile_service()
        assert isinstance(svc.runtime, rc_deps.ReconcileRuntimeDeps)
        assert isinstance(svc.state, rc_deps.ReconcileStateDeps)
        assert isinstance(svc.coordination, rc_deps.ReconcileCoordinationDeps)
        assert isinstance(svc.notification, rc_deps.ReconcileNotificationDeps)
        assert isinstance(svc.action, rc_deps.ReconcileActionDeps)

    def test_factory_builds_fresh_service(self):
        assert pm._reconcile_service() is not pm._reconcile_service()

    def test_factory_builds_fresh_bundles(self):
        svc1 = pm._reconcile_service()
        svc2 = pm._reconcile_service()
        for name in ('runtime', 'state', 'coordination', 'notification',
                     'action'):
            assert getattr(svc1, name) is not getattr(svc2, name)


class TestSeamIdentityThroughBundles:
    def test_state_seams_identity(self):
        svc = pm._reconcile_service()
        assert svc.state.load is pm._load
        assert svc.state.save is pm._save
        assert svc.state.wcr is pm._was_closed_recently
        assert svc.state.mc is pm._mark_closed
        assert svc.state.posid is pm._position_id

    def test_pg_identity(self):
        svc = pm._reconcile_service()
        assert svc.notification.pg is pm._pg_record_event

    def test_tg_identity(self):
        svc = pm._reconcile_service()
        assert svc.notification.tgt is pm._TG_TOKEN
        assert svc.notification.tgc is pm._TG_CHAT_ID

    def test_lock_identity(self):
        svc = pm._reconcile_service()
        assert svc.coordination.lacq is pm._lock_acquire
        assert svc.coordination.lrel is pm._lock_release

    def test_rdget_set_identity(self):
        svc = pm._reconcile_service()
        assert svc.coordination.rdget is pm._rget
        assert svc.coordination.rdset is pm._rset


class TestHygiene:
    def test_no_import_time_bundle(self):
        src = open('position_reconcile/deps.py').read()
        assert '__getattr__' not in src
        assert 'DEFAULT_' not in src

    def test_no_service_singleton(self):
        before = set(threading.enumerate())
        import position_reconcile
        import position_reconcile.deps
        assert before == set(threading.enumerate())

    def test_no_reverse_import(self):
        for path in ('position_reconcile/deps.py',
                     'position_reconcile/service.py'):
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert 'position_manager' not in str(node), path
                elif isinstance(node, ast.ImportFrom):
                    assert 'position_manager' not in (node.module or ''), path

    def test_notification_unify_prohibited(self):
        """禁止统一 persist_and_notify()（PMB-26 frozen）。"""
        src = open('position_reconcile/deps.py').read()
        code = ast.parse(src)
        assert not any(
            getattr(n, 'id', '') == 'persist_and_notify' or
            getattr(n, 'id', '') == 'NotificationGateway'
            for n in ast.walk(code) if isinstance(n, ast.Name))

    def test_ghost_queue_not_added(self):
        src = open('position_reconcile/deps.py').read()
        for token in ('ghost_queue', '_RECENTLY_GHOSTED'):
            assert token not in src, token
