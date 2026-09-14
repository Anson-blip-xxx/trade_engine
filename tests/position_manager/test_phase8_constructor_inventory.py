"""P8-05A：constructor inventory 快照（AST：冻结 seams 名而不 freeze 数量）。"""
from __future__ import annotations

import ast
import re

import pytest

import shared.position_manager as pm

# P8-05B 将改 constructor；本 snapshot 冻结"注入位名字+语义"而非数量
EXPECTED = {
    '_protection_service': {'cancel_all_fn', 'cancel_id_fn', 'enqueue_fn',
                            'place_algo_sl_fn', 'start_worker_fn'},
    '_state_service': {'marker_delete', 'marker_get', 'marker_set',
                       'sandbox_check', 'state_port'},
    '_lifecycle_service': {'acx', 'ci', 'close_fn', 'clr', 'cxa', 'enq',
                           'exec_fn', 'lce', 'load', 'log', 'mc', 'now',
                           'pg', 'pi', 'posid', 'rq', 's6', 'sandbox',
                           'save', 'wcr', 'wkr'},
    '_reconcile_service': {'exf', 'gpx', 'lacq', 'lgt', 'load', 'lrel',
                           'mc', 'now', 'pg', 'pid', 'posid', 'rdget',
                           'rdset', 'rqst', 's6', 'sandbox', 'save', 'sk',
                           'tgc', 'tgt', 'uid', 'wcr'},
    '_monitoring_service': {'cfg', 'cls', 'cts', 'dc', 'elm', 'fund',
                            'g1h', 'gcl', 'ghb', 'gq', 'inst', 'ldr',
                            'lkey', 'lkfn', 'load', 'log', 'lttl', 'm1',
                            'mc', 'oc', 'oe', 'oofn', 'pc', 'pp', 'pt',
                            'rq', 's6', 'save', 'shb', 'stag', 'summ',
                            'swlu', 'trgt', 'us', 'wcr', 'wsf', 'wsl',
                            'wsp', 'wst'},
}


def extract(name):
    src = open('shared/position_manager.py').read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            kws = set()
            for st in ast.walk(node):
                if isinstance(st, ast.Call):
                    for kw in st.keywords:
                        if kw.arg:
                            kws.add(kw.arg)
            return kws
    return set()


class TestConstructorInventory:
    @pytest.mark.parametrize('name', sorted(EXPECTED))
    def test_injected_seams_frozen_by_name(self, name):
        assert EXPECTED[name] <= extract(name), name

    def test_monitoring_largest_seams_present(self):
        """Monitoring 40 个注入位按责任域全名（数量允许未来 bundle）"""
        kws = extract('_monitoring_service')
        assert all(s in kws for s in ('fund', 'cls', 'load', 'save',
                                      'gcl', 'wsp', 'ldr', 'm1'))
        assert len(kws) >= 36        # ≥40 当前 → P8-05B 允许减少

    def test_lifecycle_domains_present(self):
        kws = extract('_lifecycle_service')
        assert {'exec_fn', 'ci', 'pi'} <= kws    # execution
        assert {'mc', 'clr', 'wcr'} <= kws        # marker
        assert {'wkr', 'enq', 'acx', 'cxa'} <= kws  # protection
        assert {'close_fn', 's6', 'sandbox', 'pg', 'lce'} <= kws  # action

    def test_reconcile_notification_asymmetry_seams(self):
        kws = extract('_reconcile_service')
        assert {'pg', 'tgt', 'tgc'} <= kws   # TG/PG 吞错不对称（frozen）
        assert {'rdset', 'rdget'} <= kws      # alert 30s/24h

    def test_no_di_framework_tokens(self):
        src = open('shared/position_manager.py').read()
        for token in ('Injector', 'Container', 'ServiceLocator',
                      'DI_CONTAINER', 'pydantic'):
            assert token not in src, token

    def test_no_import_time_deps_capture(self):
        """无 `DEFAULT_..._DEPS = ...` module 常量背书。"""
        for path in ('shared/position_manager.py',
                     'position_monitoring/service.py',
                     'position_reconcile/service.py',
                     'position_lifecycle/service.py'):
            src = open(path).read()
            for token in ('DEFAULT_MONITORING_DEPS', 'DEFAULT_LIFECYCLE_DEPS',
                          'DEFAULT_RECONCILE_DEPS'):
                assert token not in src, (path, token)
