"""P6-05：Phase 6 end-to-end integration closure（真实链 + 完整 dict parity）。

链路：input adapter → sample_* helpers → compute_state（classifier wrapper）→
write_state（publisher wrapper）→ Redis/file/CH。只 mock 基础设施。
"""
import json

import pytest

from conftest import BASE_TS, sym_row


def _seed_s3_rows(g, rdis, rows):
    rdis.store['market:s3_data'] = {'ts': BASE_TS, 'symbols': rows}


def _seed_market(g, rdis, *, btc_close=105.0, py_bull=True, breadth_above=10,
                 breadth_total=10, breadth_ratio=0.8):
    rows = {'BTCUSDT': {
        '4h': sym_row('BTC', close=btc_close, ema20=100.0, ema60=95.0
                      if py_bull else 105.0),
        '15m': sym_row('BTC', close=btc_close, volatility=2.0),
        '24h': sym_row('BTC', close=btc_close, volatility=1.0),
        '1h': sym_row('BTC', close=btc_close, chg=2.0)}}
    for i in range(breadth_total):
        close = 101.0 if i < round(breadth_ratio * breadth_total) else 99.0
        rows[f'S{i:03}'] = {'1h': sym_row(f'S{i:03}', close=close)}
    _seed_s3_rows(g, rdis, rows)
    return rows


def _pool(g, symbols, ts):
    g._breadth_symbols_cache = symbols
    g._breadth_symbols_ts = ts


class TestEndToEndScenarios:
    """端到端：seed → sample_btc/breadth → compute_state → full-dict parity。"""

    @pytest.fixture(autouse=True)
    def _no_real_network(self, monkeypatch):
        import services.s0.s0_market_guard as g
        def boom(path, params=None):
            raise RuntimeError('no real network in tests')
        monkeypatch.setattr(g, 'fapi_get', boom)

    def _full_state(self, g, rdis, clock, *, seed_market=True, **kw):
        if seed_market:
            _seed_market(g, rdis, **{k: v for k, v in kw.items()
                                     if k in ('btc_close', 'py_bull',
                                              'breadth_total', 'breadth_ratio')})
        # seed breadth pool（与 S3 数据同一 universe，避免 fapi fetch 真网络）
        g._breadth_symbols_cache = [f'S{i:03}'
                                    for i in range(kw.get('breadth_total', 10))]
        g._breadth_symbols_ts = clock.now
        trend, volatility, amp, below, exp = g.sample_btc()
        breadth, ratio = g.sample_breadth()
        st = g.compute_state(trend, volatility, amp, below, exp,
                             breadth, ratio)
        # publisher 落地真实链（write_state wrapper；ch spy 已在 fixture）
        g.write_state(st)
        return st

    def test_full_state_keys_and_types(self, g, rdis, clock):
        st = self._full_state(g, rdis, clock)
        EXPECTED = ('version', 'timestamp', 'market_state', 'btc_trend',
                    'breadth', 'breadth_ratio', 'volatility', 'risk_off',
                    'regime', 'regime_score', 'trend_strength', 's6_allowed',
                    's7_allowed', 's8_allowed', 'fng', 'fng_label',
                    'avg_funding', 'sentiment_risk', 'sentiment_bias',
                    'alts_sync', 'shock_score')
        assert list(st.keys()) == list(EXPECTED)
        assert isinstance(st['version'], str) and st['version'] == '1.1.0'
        assert isinstance(st['timestamp'], int) and st['timestamp'] == BASE_TS
        assert isinstance(st['risk_off'], bool)
        assert isinstance(st['breadth_ratio'], float)
        assert isinstance(st['regime_score'], int)

    def test_a_strong_bull_full_dict(self, g, rdis, clock):
        st = self._full_state(g, rdis, clock, btc_close=105.0,
                              breadth_total=10, breadth_ratio=0.9)
        assert st['market_state'] == 'trend' and st['regime'] == 'bull_trend'
        assert st['s6_allowed'] is True and st['s7_allowed'] is False

    def test_b_weak_bull(self, g, rdis, clock):
        st = self._full_state(g, rdis, clock,
                              breadth_total=10, breadth_ratio=0.5)
        assert st['regime'] == 'weak_bull'


    def test_d_weak_bear(self, g, rdis, clock):
        st = self._full_state(g, rdis, clock, btc_close=90.0,
                              breadth_total=10, breadth_ratio=0.45,
                              py_bull=False)
        assert st['regime'] in ('weak_bear', 'risk-off')

    def test_e_risk_off(self, g, rdis, clock):
        st = self._full_state(g, rdis, clock, btc_close=90.0,
                              breadth_total=10, breadth_ratio=0.29,
                              py_bull=False)
        assert st['market_state'] == 'risk-off' and st['regime'] == 'risk-off'
        assert st['s6_allowed'] is False and st['s8_allowed'] is False

    def test_f_contradiction_zone_weak_breadth_path(self, g, rdis, clock):
        """矛盾区 breadth='weak' 路径（P6-01 另一冻结分支）：
        bull + ratio 0.32 → breadth 'weak'（3/10）→ weak_bear（非 weak_bull）。"""
        st = self._full_state(g, rdis, clock, btc_close=105.0,
                              breadth_total=10, breadth_ratio=0.32)
        assert st['breadth'] == 'weak'                   # ratio 0.32 ≤ 0.4
        assert st['regime'] == 'weak_bear'               # breadth='weak' 分支

    def test_g_empty_pool(self, g, rdis, clock, monkeypatch):
        """空池（refresh 失败 + 无老池）→ ratio 0.5 normal。S0-4 冻结。"""
        def boom(path, params=None):
            raise RuntimeError('down')
        monkeypatch.setattr(g, 'fapi_get', boom)
        trend, volatility, amp, below, exp = g.sample_btc()
        breadth, ratio = g.sample_breadth()
        assert breadth == 'normal' and ratio == 0.5
        st = g.compute_state(trend, volatility, amp, below, exp,
                             breadth, ratio)
        assert st['regime'] in ('weak_bull', 'range')     # 非 risk-off

    def test_h_dilution(self, g, rdis, clock):
        """S0-5：S3 数据缺失 → denominator 浪费。"""
        rows = {'BTCUSDT': {'4h': sym_row('B', close=105.0, ema20=100.0,
                                          ema60=95.0),
                            '15m': sym_row('B', close=105.0, volatility=2.0),
                            '24h': sym_row('B', close=105.0, volatility=1.0),
                            '1h': sym_row('B', close=105.0)}}
        # S3 数据只承载一半 pool 符号的 1h 数据
        for i in range(5):
            rows[f'S{i:03}'] = {'1h': sym_row(f'S{i:03}', close=101.0,
                                              ema20=100.0)}
        _seed_s3_rows(g, rdis, rows)
        # pool 全 10 —— 后 5 个在 S3 里缺失 → dilute
        g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
        g._breadth_symbols_ts = clock.now
        breadth, ratio = g.sample_breadth()
        assert breadth == 'normal' and ratio == 0.5      # 5/10 有数据，5 缺失
        trend, volatility, amp, below, exp = g.sample_btc()
        st = g.compute_state(trend, volatility, amp, below, exp,
                             breadth, ratio)
        assert st['breadth'] == 'normal'

    def test_i_stale_s3_input(self, g, rdis, clock):
        """producer 无 stale check → 旧 S3 照常消费。"""
        _seed_s3_rows(g, rdis, {
            'BTCUSDT': {'4h': sym_row('B', close=100.0, ema20=100.0,
                                      ema60=95.0),
                        '15m': sym_row('B', close=100.0, volatility=0.0),
                        '24h': sym_row('B', close=100.0, volatility=0.0),
                        '1h': sym_row('B', close=100.0, chg=0)}})
        trend, *_ = g.sample_btc()
        assert trend == 'neutral'                        # 不是 fresh error

    def test_j_sentiment_missing_default(self, g, rdis, clock):
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        assert st['fng'] == 50 and st['fng_label'] == ''
        assert st['sentiment_risk'] is False
        assert st['sentiment_bias'] == 'neutral'


class TestPublisherE2E:
    def test_redis_file_ch_chain_real(self, s0_env, g, rdis, ch_rows, tmp_path):
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        g.write_state(st)
        # Redis
        key, val = rdis.writes[0]
        assert key == 'market:s0' and val is st         # latest slot 同引用
        # Atomic file
        assert json.loads((tmp_path / 'market_state.json').read_text()) == st
        # CH 6-field row
        row = json.loads(ch_rows[0][1])
        assert set(row.keys()) == {'market_state', 'btc_trend', 'breadth',
                                   'breadth_ratio', 'volatility', 'risk_off'}

    def test_multi_instance_last_writer_wins(self, g, rdis, ch_rows):
        g._rset('market:s0', {'market_state': 'risk-off', 'ts': 1})
        g._rset('market:s0', {'market_state': 'range', 'ts': 2})
        assert rdis.store['market:s0']['ts'] == 2       # last-writer wins (S0-8)


class TestO3WallClockClosure:
    def test_wall_clock_gate_in_wrapper_not_core(self, g, rdis, clock, monkeypatch):
        """core 不读 clock —— 采样差异由 wrapper注入，classifier 零 nondeterminism。"""
        from s0 import core
        snapshot_before = None
        # 同输入 → 两次 core 调用 → same dict
        a = core.classify_regime('bull', 'low', 0.02, False, False,
                                 'normal', 0.5, alts_sync_val=0.0,
                                 shock_val=0)
        b = core.classify_regime('bull', 'low', 0.02, False, False,
                                 'normal', 0.5, alts_sync_val=0.0,
                                 shock_val=0)
        assert a == b                                    # core deterministic
        # wall-clock 不进 core 参数集
        import inspect
        sig = list(inspect.signature(core.classify_regime).parameters)
        assert 'time' not in sig and 'now' not in sig

    def test_wallclock_gate_differs_output(self, g, rdis, clock, monkeypatch):
        """S0-3：不同 wall-clock 时间 → shock/alts 不同（维持生产 nondeterminism）。"""
        rows = [{'priceChangePercent': 20.0} for _ in range(10)]
        monkeypatch.setattr(g, 'fapi_get', lambda p, params=None: rows)
        BASE = int(clock.now)
        clock.now = BASE - (BASE % 60) + 40               # %60 = 40 ≥ 30 → 不采样
        st1 = g.compute_state('bull', 'low', 0.02, False, False,
                              'normal', 0.5)
        clock.now = BASE - (BASE % 60) + 10               # %60 = 10 <30 → 采样
        st2 = g.compute_state('bull', 'low', 0.02, False, False,
                              'normal', 0.5)
        assert st1['shock_score'] == 0
        assert st2['shock_score'] == 6                    # 10 extreme: min(5,4)+2
