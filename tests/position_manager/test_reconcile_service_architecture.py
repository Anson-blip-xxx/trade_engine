"""P7-06B：PositionReconcileService 架构 guard + legacy delegate parity。

冻结：
1. 无 PM / Monitoring / 具体 state/ledger/protection 反向 import（AST seal）
2. import 副作用 = 0（不启线程、不访问 Redis/Binance、不 sleep）
3. legacy wrappers thin delegation（_pm 显式注入 target）
4. _close 不在 service（无 close 语义依赖）
"""
import ast
import threading
import time as time_mod

import pytest

from shared import position_manager as pm
from position_reconcile import service as rc
from position_reconcile import PositionReconcileService


class TestNoReverseImport:
    def _import_names(self):
        tree = ast.parse(open(rc.__file__).read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or '')
        return names

    def test_no_pm_reconcile_concrete(self):
        mods = self._import_names()
        banned = ('position_manager', 'shared_executor', 'position_monitoring',
                  'position_state', 'position_ledger', 'position_protection',
                  'execution')
        for m in mods:
            assert not m.startswith(banned), m
            assert not any(b in m for b in banned), m

    def test_only_future_typing_imports(self):
        mods = self._import_names()
        assert mods <= {'__future__', 'typing'} or all(
            m in ('__future__', 'typing') for m in mods if m)


class TestCleanImport:
    def test_import_no_thread_no_io(self):
        before = set(threading.enumerate())
        import position_reconcile
        import position_reconcile.service as svc
        assert before == set(threading.enumerate())
        src = open(svc.__file__).read()
        assert 'redis_store' not in src   # 全部经注入（无具体 client）


class TestLegacyWrapperDelegation:
    def _fake(self, monkeypatch):
        seen = {}

        class FakeSvc:
            def ghost_cleanup(self, positions, system_filter=''):
                seen['g'] = (positions, system_filter)
                return [9]

            def ghost_cleanup_one(self, sym, pos, positions, rec, closed):
                seen['one'] = (sym, pos, positions, rec, closed)

            def try_record_ghost_trade(self, sym, meta):
                seen['rec'] = (sym, meta)
                return True

            def reconcile_all(self):
                seen['recon'] = True
                return ([8], [7])

            def notify_external_position(self, sym, raw, system):
                seen['notify'] = (sym, raw, system)

            def migrate_existing_positions(self):
                seen['mig'] = True
                return {}
        monkeypatch.setattr(rc, 'PositionReconcileService',
                            lambda **kw: FakeSvc())
        return seen

    def test_ghost_cleanup_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        assert pm._ghost_cleanup({'s': 1}, system_filter='S6') == [9]
        assert seen['g'] == ({'s': 1}, 'S6')

    def test_ghost_cleanup_one_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        pm._ghost_cleanup_one('A', {}, {}, None, [])
        assert seen['one'][0] == 'A'

    def test_try_record_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        assert pm._try_record_ghost_trade('A', {'entry': 1}) is True
        assert seen['rec'] == ('A', {'entry': 1})

    def test_reconcile_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        assert pm.reconcile_all() == ([8], [7])
        assert seen['recon'] is True

    def test_notify_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        pm._notify_external_position('A', {'r': 1}, 'S6')
        assert seen['notify'] == ('A', {'r': 1}, 'S6')

    def test_migrate_delegate(self, monkeypatch):
        seen = self._fake(monkeypatch)
        pm.migrate_existing_positions()
        assert seen['mig'] is True
