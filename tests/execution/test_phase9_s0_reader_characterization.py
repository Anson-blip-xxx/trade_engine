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
    def test_s0_risk_off_market_state_blocks_open(self, monkeypatch):
        """P9-01B 修复回归 guard：producer-shaped risk-off 现在**阻断**。"""
        payload = _producer_payload()          # market_state == 'risk-off'
        assert payload.get('market_mode') is None   # key mismatch 证据保留
        recorded = []

        def fake_log(name, msg):
            recorded.append((name, msg))
        monkeypatch.setattr(se, '_rget', lambda key: payload if
                            key == 'market:s0' else None)
        monkeypatch.setattr(se, '_log', fake_log)
        assert se.market_allows_trading('S6', 'LONG') is False
        assert any('跳过开仓' in m for _, m in recorded)

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

    def test_both_keys_conflict_new_key_wins(self, monkeypatch):
        """P9-01B authority：market_state 优先；B/A/C 矩阵。"""
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': 'risk-off', 'market_mode': 'normal'})
        assert se.market_allows_trading('S6', 'LONG') is False   # A
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': 'range', 'market_mode': 'risk_off'})
        assert se.market_allows_trading('S6', 'LONG') is True    # B
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': 'trend', 'market_mode': 'risk_off'})
        assert se.market_allows_trading('S6', 'LONG') is True    # C

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


class TestNewKeySemantics:
    def test_none_payload_fail_open(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: None)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_empty_payload_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_market_state_none_authoritative_allows(self, monkeypatch):
        """presence != truthiness：market_state=None 不逆 fallback。"""
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': None, 'market_mode': 'risk_off'})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_market_state_empty_authoritative_allows(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {
            'market_state': '', 'market_mode': 'risk_off'})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_unknown_market_state_fail_open(self, monkeypatch):
        monkeypatch.setattr(se, '_rget',
                            lambda key: {'market_state': 'WTF'})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_missing_both_keys_fail_open(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {'foo': 1})
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_fetch_exception_fail_open_keeps(self, monkeypatch):
        def boom(key):
            raise RuntimeError('redis')
        monkeypatch.setattr(se, '_rget', boom)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_call_count_unchanged(self, monkeypatch):
        hits = []
        monkeypatch.setattr(se, '_rget',
                            lambda key: hits.append(key) or {})
        se.market_allows_trading('S6', 'LONG')
        assert hits == ['market:s0']     # 仍一次 fetch（同一 payload 内 fallback）

    def test_hyphen_value_blocks(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {'market_state': 'risk-off'})
        assert se.market_allows_trading('S6', 'LONG') is False

    def test_legacy_underscore_value_blocks(self, monkeypatch):
        monkeypatch.setattr(se, '_rget', lambda key: {'market_state': 'risk_off'})
        assert se.market_allows_trading('S6', 'LONG') is False

    def test_s0_producer_vocab_trend_range_allow(self, monkeypatch):
        for v in ('trend', 'range'):
            monkeypatch.setattr(se, '_rget',
                                lambda key, v=v: {'market_state': v})
            assert se.market_allows_trading('S6', 'LONG') is True


class TestS6S8OpenGateIntegration:
    def _s6_name(self):
        try:
            from strategies import S6 as m6
            return m6.NAME
        except BaseException:
            return 'S6'

    def _s8_name(self):
        try:
            from strategies import S8 as m8
            return m8.NAME
        except BaseException:
            return 'S8'

    def test_s6_open_gate_blocked_under_producer_risk_off(self, monkeypatch):
        """producer-shaped risk-off → S6 open gate 阻断（不真实下单；
        reader seam 级，S6 main-loop `_mkt_ok` 实际消费此函数）。"""
        payload = _producer_payload()
        monkeypatch.setattr(se, '_rget', lambda key: payload if
                            key == 'market:s0' else None)
        monkeypatch.setattr(se, '_log', lambda n, m: None)
        assert se.market_allows_trading(self._s6_name(), 'LONG') is False

    def test_s8_short_gate_blocked(self, monkeypatch):
        payload = _producer_payload()
        monkeypatch.setattr(se, '_rget', lambda key: payload if
                            key == 'market:s0' else None)
        monkeypatch.setattr(se, '_log', lambda n, m: None)
        assert se.market_allows_trading(self._s8_name() if False else self._s8_name(), 'SHORT') if False else             se.market_allows_trading(self._s8_name(), 'SHORT') is False

    def test_close_partial_unaffected_seam(self):
        """close/partial 链不调用 market_allows_trading（blast radius）。"""
        import inspect
        lc_src = inspect.getsource(
            __import__('position_lifecycle.service', fromlist=['x']))
        assert 'market_allows_trading' not in lc_src
        se_src = open('strategies/shared_executor.py').read()
        assert se_src.count("market_allows_trading(") == 1  # def
