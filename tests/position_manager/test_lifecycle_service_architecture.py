"""P7-07B：PositionLifecycleService 架构 guard + legacy delegate parity。

冻结：
1. 无 PM / monitoring / reconcile / 具体 state/ledger/protection 反向 import（AST seal）
2. import 副作用 = 0
3. 4 个 legacy wrapper（open_position/close_position/_close/_partial_close）
   thin delegation
4. close_position 经注入 close_fn 保留 pm seam（monkeypatch 兼容）
"""
import ast
import threading
import time as time_mod

import pytest

from shared import position_manager as pm
from position_lifecycle import service as lc
from position_lifecycle import PositionLifecycleService


class TestNoReverseImport:
    def _import_names(self):
        tree = ast.parse(open(lc.__file__).read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or '')
        return names

    def test_no_banned_imports(self):
        banned = ('position_manager', 'shared_executor', 'position_monitoring',
                  'position_reconcile', 'position_state', 'position_ledger',
                  'position_protection', 'execution')
        for m in self._import_names():
            assert not m.startswith(banned), m
            assert not any(b in m for b in banned), m

    def test_zero_runtime_imports(self):
        assert self._import_names() <= {'__future__', 'typing',
                                        'scripts.sandbox'}


class TestCleanImport:
    def test_import_no_thread_no_io(self):
        before = set(threading.enumerate())
        import position_lifecycle
        import position_lifecycle.service
        assert before == set(threading.enumerate())
        src = open(lc.__file__).read()
        assert 'redis_store' not in src      # 全部经注入


class TestLegacyWrapperDelegation:
    def _fake(self, monkeypatch):
        seen = {}

        class FakeSvc:
            def open_position(self, *a, **kw):
                seen['open'] = (a, kw)
                return True

            def close_position(self, symbol, reason):
                seen['close_pos'] = (symbol, reason)
                return True

            def close(self, symbol, pos, price, reason, positions, *,
                      force=False):
                seen['close'] = (symbol, reason, force)
                return True

            def partial_close(self, *a, **k):
                seen['partial'] = a
        monkeypatch.setattr(lc, 'PositionLifecycleService',
                            lambda **kw: FakeSvc())
        return seen

    def test_open_delegates(self, monkeypatch):
        seen = self._fake(monkeypatch)
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 2.0, 3, 0.9,
                              system='S6')
        assert ok is True
        assert seen['open'][0][:5] == ('TUSDT', 'SHORT', 1.0, 2.0, 3)

    def test_close_position_delegates(self, monkeypatch):
        seen = self._fake(monkeypatch)
        assert pm.close_position('AUSDT', '手动') is True
        assert seen['close_pos'] == ('AUSDT', '手动')

    def test_pm_close_delegates(self, monkeypatch):
        seen = self._fake(monkeypatch)
        assert pm._close('A', {}, 1.0, '硬止损', {}) is True
        assert seen['close'] == ('A', '硬止损', False)  # 默认 force=False

    def test_pm_partial_delegates(self, monkeypatch):
        seen = self._fake(monkeypatch)
        pm._partial_close('A', {}, 1.0, 0.5, 2.0, {})
        assert seen['partial'][0] == 'A'


class TestSeamPreserved:
    def test_close_position_uses_pm_close_fn(self, monkeypatch):
        """close_position 必须经由 pm `_close` wrapper（patch 荣誉）。"""
        calls = []
        monkeypatch.setattr(pm, '_load', lambda: {
            'AUSDT': {'entry': 1.0, 'qty': 1.0}})
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 1.0, None, None, None, None))

        def fake_close(sym, pos, price, reason, positions, **kw):
            calls.append((sym, reason, kw))
            return True
        monkeypatch.setattr(pm, '_close', fake_close)
        assert pm.close_position('AUSDT', '手动') is True
        assert calls == [('AUSDT', '手动', {'force': True})]
