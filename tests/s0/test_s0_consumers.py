"""P6-01：消费者侧（S0-1/S0-2/stale/S7/写序/CH/file/multi-instance）。"""
import json
import time

import pytest

from conftest import BASE_TS

# ═══════════════════════════════════════════════════════════════
#  S0-1：market_state vs market_mode 键名失配 → fail-open（KNOWN FROZEN）
# ═══════════════════════════════════════════════════════════════

S0_PAYLOAD = {
    'version': '1.1.0', 'timestamp': BASE_TS, 'market_state': 'risk-off',
    'btc_trend': 'bear', 'breadth': 'weak', 'breadth_ratio': 0.12,
    'volatility': 'high', 'risk_off': True, 'regime': 'risk-off',
    'regime_score': -7, 'trend_strength': 0, 's6_allowed': False,
    's7_allowed': False, 's8_allowed': False,
}


class TestS01FailOpenFrozen:
    def test_risk_off_state_still_trades(self, monkeypatch):
        """真实 consumer path：market_allows_trading 读 market_mode（S0 不写）
        → risk-off 市场下仍返回 True（fail-open）。"""
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: S0_PAYLOAD)
        monkeypatch.setattr(se, '_log', lambda *a, **k: None)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_market_mode_honored_if_written(self, monkeypatch):
        """对照：显式 market_mode 存在时 gate 生效（机制在、键不在）。"""
        from strategies import shared_executor as se
        payload = dict(S0_PAYLOAD, market_mode='risk_off')
        monkeypatch.setattr(se, '_rget', lambda k: payload)
        monkeypatch.setattr(se, '_log', lambda *a, **k: None)
        assert se.market_allows_trading('S6', 'LONG') is False

    def test_redis_down_fail_open(self, monkeypatch):
        from strategies import shared_executor as se

        def boom(k):
            raise RuntimeError('down')
        monkeypatch.setattr(se, '_rget', boom)
        monkeypatch.setattr(se, '_log', lambda *a, **k: None)
        assert se.market_allows_trading('S6', 'LONG') is True

    def test_malformed_string_payload_raises_attribute_error(self, monkeypatch):
        """冻结：字符串 payload → get_market_state 原样返回 →
        market_allows_trading 上 .get 抛 AttributeError（无统一错误处理）。"""
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: 'not-a-dict')
        monkeypatch.setattr(se, '_log', lambda *a, **k: None)
        with pytest.raises(AttributeError):
            se.market_allows_trading('S8', 'SHORT')

    def test_no_stale_gate_in_se_consumer(self, monkeypatch):
        """se 读取端无 stale 检查——任意老 snapshot 照用（事实）。"""
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_rget', lambda k: S0_PAYLOAD)
        assert se.get_market_state()['market_state'] == 'risk-off'


# ═══════════════════════════════════════════════════════════════
#  S0-2：is_system_allowed 缺键 → True（注释自称"保守默认"）
# ═══════════════════════════════════════════════════════════════

class TestS02DefaultAllow:
    @pytest.fixture(autouse=True)
    def _fresh_reader_time(self, monkeypatch):
        """s0_reader 的时间 gate 需 fake（payload ts 是 BASE 常量）。"""
        import services.s0.s0_reader as r
        self.r = r
        monkeypatch.setattr(r, 'time', type('T', (), {
            'time': staticmethod(lambda: BASE_TS + 10)}))     # fresh

    def test_missing_state_defaults_true(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: None)
        assert self.r.is_system_allowed(6) is True   # S0-2 "保守默认"=True
        assert self.r.is_system_allowed(8) is True

    def test_deny_respected_when_present(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: S0_PAYLOAD)
        assert self.r.is_system_allowed(6) is False   # 显式 deny 生效
        assert self.r.is_system_allowed(8) is False

    def test_partial_state_missing_key_true(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget',
                            lambda k: {'market_state': 'risk-off'})
        assert self.r.is_system_allowed(6) is True

    def test_malformed_state_true(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: [])
        assert self.r.is_system_allowed(6) is True


# ═══════════════════════════════════════════════════════════════
#  s0_reader stale / version gate（S7 兼容契约）
# ═══════════════════════════════════════════════════════════════

class TestS0ReaderStaleGate:
    @pytest.fixture(autouse=True)
    def _reader(self):
        import services.s0.s0_reader as r
        self.r = r

    def _fake_time(self, monkeypatch, ts):
        monkeypatch.setattr(self.r, 'time', type('T', (), {
            'time': staticmethod(lambda: ts)}))

    def test_fresh_state_passes(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: S0_PAYLOAD)
        self._fake_time(monkeypatch, BASE_TS + 10)
        assert self.r.load_market_state()['market_state'] == 'risk-off'

    def test_stale_boundary_180(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: S0_PAYLOAD)
        self._fake_time(monkeypatch, BASE_TS + 179)
        assert self.r.load_market_state() is not None   # ≤180 内 fresh
        self._fake_time(monkeypatch, BASE_TS + 181)
        assert self.r.load_market_state() is None

    def test_version_gate(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget',
                            lambda k: dict(S0_PAYLOAD, version='0.9.0'))
        self._fake_time(monkeypatch, BASE_TS + 10)
        assert self.r.load_market_state() is None

    def test_fallbacks(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: None)
        assert self.r.get_regime() == 'range'          # S7 fallback
        assert self.r.get_risk_off() is False
        assert self.r.get_shock_score() == 0
        assert self.r.get_breadth_ratio() == 0.5
        assert self.r.get_btc_trend() == 'neutral'

    def test_get_regime_with_state(self, monkeypatch):
        monkeypatch.setattr(self.r, '_rget', lambda k: S0_PAYLOAD)
        self._fake_time(monkeypatch, BASE_TS + 10)
        assert self.r.get_regime() == 'risk-off'


# ═══════════════════════════════════════════════════════════════
#  write_state：Redis → 文件 → CH 三写 + 部分失败
# ═══════════════════════════════════════════════════════════════

class TestWriteState:
    def test_order_and_red_file_ch_ws(self, s0_env, g, rdis, ch_rows, tmp_path):
        """写序冻结：redis → 原子文件 → CH。"""
        state = g.compute_state('bull', 'low', 0.02, False, False,
                                'normal', 0.5)
        g.write_state(state)
        assert len(rdis.writes) == 1
        assert rdis.writes[0][0] == 'market:s0'
        assert rdis.writes[0][1]['market_state'] == 'range'
        assert json.loads((tmp_path / 'market_state.json').read_text())[
            'regime'] == 'weak_bull'   # bull+normal → weak_bull 非 range
        assert ch_rows[-1][0] == 'default.market_state_log'

    def test_redis_failure_file_and_ch_still(self, s0_env, g, rdis, ch_rows, tmp_path):
        g, fr, _fk, _ch, tmp = s0_env
        fr.set_exc = RuntimeError('redis down')
        state = g.compute_state('bull', 'low', 0.02, False, False,
                                'normal', 0.5)
        g.write_state(state)                     # redis 失败 → 吞错
        assert (tmp / 'market_state.json').exists()
        assert ch_rows[0][0] == 'default.market_state_log'

    def test_ch_failure_after_file(self, s0_env, g, rdis, ch_rows, tmp_path, monkeypatch):
        import shared.clickhouse_client as chc
        def ch_boom(table, row):
            raise RuntimeError('ch down')
        monkeypatch.setattr('shared.clickhouse_client.insert', ch_boom)
        state = g.compute_state('bull', 'low', 0.02, False, False,
                                'normal', 0.5)
        g.write_state(state)                     # CH 失败 → log warning 不抛
        assert rdis.writes and tmp_path.joinpath('market_state.json').exists()


class TestMultiInstanceLastWriterWins:
    def test_no_leader_no_coordination(self, rdis):
        rdis.store['market:s0'] = {'market_state': 'risk-off', 'ts': 1}
        rdis.store['market:s0'] = {'market_state': 'range', 'ts': 2}
        assert rdis.store['market:s0']['ts'] == 2    # 后写覆盖（B 实例）
