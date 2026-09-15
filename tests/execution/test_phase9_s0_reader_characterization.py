"""P9-01A：S0-1 reader characterization（producer/reader contract 冻结）。

⚠️ 冻结的是**当前 bug**（mismatch/fail-open），不是修复后的结果。
"""
from __future__ import annotations

import pytest

from strategies import shared_executor as se
from s0 import core as s0_core


def _producer_payload():
    """真实 S0 producer shape：classify_regime 参数（真实签名）。"""
    core = s0_core.classify_regime(
        btc_trend='bear', volatility='high', amp=2.0,
        btc_below_ema60=True, atr_expanding=True,
        breadth='middle', breadth_ratio=0.1,
        sentiment={'fng': 50})
    return dict(core)


class TestProducerContract:
    def test_s0_producer_emits_market_state_key(self):
        """冻结：S0 producer 当前写 'market_state'（P9-01A 生产零 diff）。"""
        src = open('s0/core.py').read()
        assert '"market_state":  market_state' in src
        assert 'market_mode' not in src           # 并未写 market_mode

    def test_s0_state_vocabulary_is_hyphen(self):
        """冻结：producer 写 'risk-off'（连字符），se reader 对 'risk_off' 判断。"""
        core = _producer_payload()
        assert core['market_state'] == 'risk-off'

    def test_reader_source_uses_market_mode(self):
        src = open('strategies/shared_executor.py').read()
        assert "ms.get('market_mode', 'normal')" in src


class TestReaderContract:
    def test_producer_shaped_payload_fail_open_now(self, monkeypatch):
        """核心 bug 重现：真实 S0 producer-shape payload 只含
        `market_state` —— 当前 reader 读 `market_mode` → 缺省 'normal'
        → **fail-open 允许开仓**。"""
        payload = _producer_payload()          # market_state == 'risk-off'
        assert payload.get('market_mode') is None   # key mismatch 直接证据

        recorded = []

        def fake_log(name, msg):
            recorded.append((name, msg))
        monkeypatch.setattr(se, '_rget', lambda key: payload if
                            key == 'market:s0' else None)
        monkeypatch.setattr(se, '_log', fake_log)
        assert se.market_allows_trading('S6', 'LONG') is True   # fail-open

    def test_legacy_market_mode_risk_off_blocks(self, monkeypatch):
        """legacy path 冻结：写 'market_mode'+'risk_off' → 忽略。"""
        monkeypatch.setattr(se, '_rget',
                            lambda key: {'market_mode': 'risk_off'}
                            if key == 'market:s0' else None)
        assert se.market_allows_trading('S6', 'LONG') is False  # 仅 legacy key 生效

    def test_market_mode_normal_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget',
                            lambda key: {'market_mode': 'normal'}
                            if key == 'market:s0' else None)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_missing_payload_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: None)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_empty_payload_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_both_keys_conflict_to_legacy(self, monkeypatch):
        """冻结 conflict：market_mode 优先（key 决定）；legacy VALUES wins."""
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': 'risk-off',           # 新 key（producer 语义）
            'market_mode': 'normal',              # legacy key
        })
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_unknown_value_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget',
                            lambda key: {'market_mode': 'WTF'})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_fetch_exception_fail_open(self, monkeypatch):
        def boom(key):
            raise RuntimeError('redis down')
        monkeypatch.setattr(se, '_rget', boom)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_call_count_frozen(self, monkeypatch):
        hits = []
        monkeypatch.setattr(se, '_rget',
                            lambda key: hits.append(key) or
                            {'market_mode': 'risk_off'})
        se.market_allows_trading('S6', 'LONG')
        assert hits == ['market:s0']            # 单次 fetch

    def test_blast_radius_scope(self):
        """冻结 blast radius：S6/S8 闸门调用点（open path only）。"""
        s6 = open('strategies/S6.py').read()
        s8 = open('strategies/S8.py').read()
        assert 'market_allows_trading(NAME' in s6
        assert 'market_allows_trading(NAME' in s8
