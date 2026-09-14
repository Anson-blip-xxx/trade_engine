"""P8-03：funding helper 架构 guard（leaf / 无 transport 迁移 / PM srcd）。"""
import ast
import sys
import threading

import pytest

import shared.position_manager as pm
import position_market.funding as fd


class TestLeafDependency:
    def test_imports_only_typing(self):
        tree = ast.parse(open(fd.__file__).read())
        mods = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.ImportFrom):
                mods.add(node.module or '')
        assert mods <= {'__future__', 'typing'}

    def test_no_transport_import(self):
        src = open(fd.__file__).read()
        for name in ('requests', 'redis', 'threading', 'websocket',
                     'position_manager', 'shared_executor', 'execution'):
            assert name not in src, name


class TestTransportStaysInPM:
    def test_pm_fetch_fn_present(self):
        """legacy `requests.get` transport 留 PM（C1 不迁）。"""
        assert callable(pm._funding_fetch_fn)
        src = open(pm.__file__).read()
        assert '_FAPI}/fapi/v1/premiumIndex?symbol={symbol}' in src
        assert "timeout=5" in src

    def test_helper_no_dupe_endpoint(self):
        """helper 无第二 endpoint 副本。"""
        src = open(fd.__file__).read()
        assert 'premiumIndex' not in src


class TestCleanImport:
    def test_no_thread_spawn_on_import(self):
        before = set(threading.enumerate())
        import position_market
        import position_market.funding
        assert before == set(threading.enumerate())

    def test_pm_alias(self):
        assert pm._funding_helper is fd
