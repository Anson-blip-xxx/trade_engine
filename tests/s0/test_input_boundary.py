"""P6-04：S0 Input / Snapshot boundary —— 真实路径一致性盘点。

覆盖：sample_btc/sample_breadth/sample_sentiment/sample_alts_sync/
sample_shock_score 与 adapter 路径 parity（全部 legacy expected = P6-01 黄金）。
"""
import pytest

from conftest import BASE_TS, sym_row, seed_s3


class TestS3SnapshotSemantics:
    def test_read_s3_market_shape_and_return(self, g, rdis):
        seed_s3(rdis.store, {'BTCUSDT': {'4h': sym_row('BTC', close=100.0)}})
        ad = g._s0_market_data()
        data = ad.read_s3_market()
        assert 'BTCUSDT' in data['symbols'] and 'ts' in data

    def test_read_s3_market_missing_key_empty(self, g, rdis):
        assert g._s0_market_data().read_s3_market() == {}

    def test_read_s3_market_malformed_shape(self, g, rdis):
        """data 存在但无 'symbols' → {}（legacy 分支行为）。"""
        rdis.store['market:s3_data'] = {'some': 'data'}  # non-empty 无 symbols
        assert g._s0_market_data().read_s3_market() == {}

    def test_read_s3_market_malformed_list(self, g, rdis):
        rdis.store['market:s3_data'] = ['not', 'dict']
        assert g._s0_market_data().read_s3_market() == {}

    def test_read_s3_market_redis_exception_empty(self, g, rdis):
        rdis.get_exc = RuntimeError('down')
        assert g._s0_market_data().read_s3_market() == {}

    def test_read_s3_window_same_frame_call_count(self, g, rdis):
        """每次 read_s3_window 独立 _rget（legacy `_s3_window` 镜像）。"""
        seed_s3(rdis.store, {'BTCUSDT': {'4h': sym_row('B', close=100.0)}})
        n_before = len(rdis.reads)
        ad = g._s0_market_data()
        ad.read_s3_window('BTCUSDT', '4h')
        ad.read_s3_window('BTCUSDT', '4h')
        assert len(rdis.reads) - n_before == 2             # 每次独立重读


class TestBTCParity:
    def test_btc_normal_bull(self, g, rdis, clock):
        """接近 4h ema20 bull 校验（price > ema20: 105>100 & 100>95）。"""
        seed_s3(rdis.store, {'BTCUSDT': {
            '4h': sym_row('BTC', close=105.0, ema20=100.0, ema60=95.0),
            '15m': sym_row('BTC', close=105.0, volatility=2.0),
            '24h': sym_row('BTC', close=105.0, volatility=1.0),
            '1h': sym_row('BTC', close=105.0, chg=2.0)}})
        trend, volatility, amp, below, exp = g.sample_btc()
        assert trend == 'bull' and volatility == 'normal'
        assert below is False                             # price > ema60
        assert exp is True                                # vol15=2.0 > vol24=1.0*1.3

    def test_btc_missing_all_neutral(self, g, rdis):
        trend, volatility, amp, below, exp = g.sample_btc()
        assert trend == 'neutral' and below is False and \
            exp is False and volatility == 'low'           # amp=0 → low

    def test_btc_atr_expanding_variants(self, g, rdis, clock, monkeypatch):
        for vol15, vol24, expect in [(1.4, 1.0, True), (1.3, 1.0, False),
                                     (0, 0, False)]:
            if vol24 == 0:
                rows = {'BTCUSDT': {'4h': sym_row('B', close=100.0, ema20=100.0,
                                                 ema60=95.0),
                                    '15m': sym_row('B', close=100.0, volatility=0.0),
                                    '24h': sym_row('B', close=100.0, volatility=0.0)}}
                seed_s3(rdis.store, rows)
            else:
                seed_s3(rdis.store, {'BTCUSDT': {
                    '4h': sym_row('B', close=100.0, ema20=100.0, ema60=95.0),
                    '15m': sym_row('B', close=100.0, volatility=vol15),
                    '24h': sym_row('B', close=100.0, volatility=vol24)}})
            _t, _v, _a, _b, exp = g.sample_btc()
            assert exp == expect                          # vol15 > vol24*1.3 严格

    def test_btc_below_ema60(self, g, rdis):
        seed_s3(rdis.store, {'BTCUSDT': {
            '4h': sym_row('B', close=90.0, ema20=95.0, ema60=100.0),
            '15m': sym_row('B', close=90.0, high=92.0, low=88.0),
            '24h': sym_row('B', close=90.0, volatility=0.0),
            '1h': sym_row('B', close=90.0, chg=0)}})
        trend, volatility, amp, below, exp = g.sample_btc()
        assert trend == 'bear'                            # ema20<ema60 & price<ema20
        assert below is True and exp is False             # price<ema60; vol24=0 → 不扩张
        assert amp == pytest.approx(4.0 / 90.0)           # (92-88)/90
        assert volatility == 'high'


class TestSentimentParity:
    def test_sentiment_missing_empty(self, g, rdis):
        assert g.sample_sentiment() == {}

    def test_sentiment_malformed_list_empty(self, g, rdis):
        rdis.store['market:sentiment'] = ['bad', 'shape']
        assert g.sample_sentiment() == {}                 # 非 dict → {}

    def test_sentiment_full_dict_passthrough(self, g, rdis):
        seed = {'fng': 61, 'fng_label': 'Greed', 'avg_funding': -3.8e-06,
                'sentiment_risk': False, 'bias': 'neutral'}
        rdis.store['market:sentiment'] = seed
        assert g.sample_sentiment() is seed              # 同一引用（无 copy）


class TestTickerParity:
    def test_ticker_via_fapi_get_raise_semantics(self, g, rdis, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError('net down')
        monkeypatch.setattr(g, 'fapi_get', boom)
        with pytest.raises(RuntimeError):
            g._s0_market_data().fetch_ticker_24h()

    def test_ticker_success_delegation(self, g, rdis, monkeypatch):
        seen = []
        monkeypatch.setattr(g, 'fapi_get',
                            lambda path, params=None: seen.append(path) or [
                                {'symbol': 'AAAUSDT', 'quoteVolume': '560000000.83'}])
        out = g._s0_market_data().fetch_ticker_24h()
        assert out[0]['symbol'] == 'AAAUSDT'
        assert seen == ['/fapi/v1/ticker/24hr']          # endpoint/params 不变


class TestShockAltsGatedCallBehavior:
    def test_shock_via_input_fetch(self, g, rdis, monkeypatch, clock):
        """sample_shock_score 経 adapter.fetch_ticker -- 异常 → 0 吞错。"""
        class Bad:
            def json(self):
                raise RuntimeError('net down')
        monkeypatch.setattr(g, 'fapi_get', lambda path, params=None: Bad())
        assert g.sample_shock_score() == 0               # 吞错（不修）

    def test_alts_sync_reads_s3_only(self, g, rdis):
        """sample_alts_sync 只读 market:s3_data（BTC 缺→(0.5,0,0)）。"""
        seed_s3(rdis.store, {})
        sync, following, total = g.sample_alts_sync()
        assert sync == 0.5 and following == 0 and total == 0
