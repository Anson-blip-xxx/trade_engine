"""P8-03：funding helper 迁移 parity（endpoint/解析/失败→0/call count 不变）。"""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

import shared.position_manager as pm
from position_market.funding import read_funding_rate


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class TransportSpy:
    """legacy transport spy：endpoint/params/timeout/symbol 逐字冻结。"""

    def __init__(self):
        self.calls = []

    def __call__(self, symbol):
        self.calls.append(symbol)
        return FakeResp({'lastFundingRate': '0.000123'})


class TestParsingParity:
    def test_positive_string(self):
        spy = TransportSpy()
        assert read_funding_rate('X', spy) == 0.000123
        assert spy.calls == ['X']

    def test_negative_rate(self):
        assert read_funding_rate('X', lambda s: FakeResp(
            {'lastFundingRate': '-0.001'})) == -0.001

    def test_zero_and_float_numeric(self):
        assert read_funding_rate('X', lambda s: FakeResp(
            {'lastFundingRate': '0'})) == 0.0
        assert read_funding_rate('X', lambda s: FakeResp(
            {'lastFundingRate': 0.5})) == 0.5
        assert read_funding_rate('X', lambda s: FakeResp(
            {'lastFundingRate': '2.5e-4'})) == 2.5e-4

    def test_failure_to_zero_matrix(self):
        """失败拓扑逐项：HEAD try/except 全覆盖路径 → 0。"""
        cases = [
            None,                    # transport None（no .json）
            {},                      # dict（no .json）
            'not-a-response',        # 非对象
            FakeResp({}),            # missing field
            FakeResp({'lastFundingRate': None}),   # field None → float(None)炸→0
            FakeResp({'lastFundingRate': ''}),     # empty string → float('')炸→0
            FakeResp({'lastFundingRate': 'abc'}),  # invalid numeric → 0
        ]

        def raise_transport(s):
            raise RuntimeError('net')
        cases.append(raise_transport)
        for fetch in cases:
            assert read_funding_rate('X', fetch) == 0.0, str(fetch)


class TestPMWrapperParity:
    def test_wrapper_value_parity(self, monkeypatch):
        monkeypatch.setattr(pm.requests, 'get',
                            lambda url, timeout=None: FakeResp(
                                {'lastFundingRate': '-0.00015'}))
        assert pm._get_funding_rate('TUSDT') == -0.00015

    def test_pm_wrapper_endpoint_params_frozen(self, monkeypatch):
        fired = []
        monkeypatch.setattr(pm.requests, 'get',
                            lambda url, timeout=None:
                            fired.append((url, timeout)))
        pm._get_funding_rate('DOGEUSDT')
        url, timeout = fired[0]
        assert 'premiumIndex?symbol=DOGEUSDT' in url
        assert timeout == 5

    def test_monkeypatch_seam_still_works(self, monkeypatch):
        """_monitor_one 经 P7-05B seam 消费 `_get_funding_rate`——
        patch pm wrapper 后 monitoring 走 patch 值（不被 helper 绕过）。"""
        monkeypatch.setattr(pm, '_get_funding_rate', lambda s: -0.006)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 2.0, None, None, None, None))
        monkeypatch.setattr(pm, '_get_cfg', lambda p: {
            'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
            'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {}})
        monkeypatch.setattr(pm, '_close', lambda *a, **k: True)
        pos = {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT',
               'open_time': 1.0}
        r = pm._monitor_one('TUSDT', pos, {'TUSDT': pos})
        assert r is not None and r[0].startswith('资金费率过高')

    def test_monitor_funding_call_count_unchanged(self, monkeypatch):
        class Knob:
            def __init__(self):
                self.funding = 0.0
                self.n = 0
        n = Knob()

        def fake_fund(s):
            n.n += 1
            return n.funding
        monkeypatch.setattr(pm, '_get_funding_rate', fake_fund)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 1.99, None, None, None, None))
        monkeypatch.setattr(pm, '_get_cfg', lambda p: {
            'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
            'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {}})
        monkeypatch.setattr(pm, '_get_data_cache', lambda: type('D', (), {
            'get_klines': staticmethod(lambda *a: [])})())
        monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                            lambda k, s: False)
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda *a, **k: False)
        monkeypatch.setattr(pm, '_update_stop_loss', lambda *a, **k: None)
        monkeypatch.setattr(pm, '_peak_pullback_check',
                            lambda *a, **k: None)
        pos = {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT', 'sl': 2.5,
               'open_time': 1.0}
        pm._monitor_one('TUSDT', pos, {'TUSDT': pos})
        assert n.n == 1                      # 单次调用（no prefetch/cache）


class TestCleanImport:
    def test_subprocess_clean_import(self):
        res = subprocess.run(
            [sys.executable, '-c',
             "import os; os.environ['PM_NO_WS']='1'; "
             "import position_market.funding;"
             "print(position_market.funding.read_funding_rate.__name__)"],
            capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == 'read_funding_rate'

    def test_import_no_thread_no_io(self):
        before = set(threading.enumerate())
        import position_market.funding
        assert before == set(threading.enumerate())
        assert 'requests' not in open(
            position_market.funding.__file__).read()
