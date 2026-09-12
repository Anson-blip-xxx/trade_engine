"""P6-01：breadth/top50 池 golden（稀释/空池/6h cache/严格关系）。"""
import pytest

from conftest import sym_row, seed_s3, BASE_TS

B = 1_700_000_000


def _bull(i):
    return sym_row(f'S{i:03}', close=101.0, ema20=100.0)


def _bear(i):
    return sym_row(f'S{i:03}', close=99.0, ema20=100.0)


def _seed_pool(values):
    """tools top-50 by 成交量（模拟 get_breadth_symbols 输出）。"""
    import strategies.s0.s0_market_guard as g
    return g


class TestBreadthRatio:
    def test_all_bullish(self, g, rdis):
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': {
            **{f'S{i:03}': {'1h': _bull(i)} for i in range(10)}}}
        g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert breadth == 'strong' and ratio == 1.0

    def test_half_split_normal(self, g, rdis):
        rows = {f'S{i:03}': {'1h': _bull(i) if i % 2 == 0 else _bear(i)}
                for i in range(10)}
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': rows}
        g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert breadth == 'normal' and ratio == 0.5

    def test_all_bearish_weak(self, g, rdis):
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': {
            **{f'S{i:03}': {'1h': _bear(i)} for i in range(10)}}}
        g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert breadth == 'weak' and ratio == 0.0

    def test_missing_s3_symbols_dilute_denominator_only(self, g, rdis):
        """S0-5 冻结：5/10 缺数据 → 全计入 total、不计入 above → ratio 减半。"""
        rows = {f'S{i:03}': {'1h': _bull(i)} for i in range(5)}   # 仅 5 个有数据
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': rows}
        g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert ratio == 0.5 and breadth == 'normal'              # 稀释=变 normal

    def test_invalid_ema20_zero_not_counted(self, g, rdis):
        rows = {'S001': {'1h': sym_row('S001', close=101, ema20=0.0)},
                'S002': {'1h': _bull(2)}}
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': rows}
        g._breadth_symbols_cache = ['S001', 'S002']
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert ratio == 0.5                                       # ema20=0 → close>ema20>0 False

    def test_close_equal_ema_not_counted(self, g, rdis):
        rows = {'S001': {'1h': sym_row('S001', close=100.0, ema20=100.0)},
                'S002': {'1h': _bull(2)}}
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': rows}
        g._breadth_symbols_cache = ['S001', 'S002']
        g._breadth_symbols_ts = B
        breadth, ratio = g.sample_breadth()
        assert ratio == 0.5                                       # 严格 > 分开判


class TestBreadthThresholds:
    def test_strong_normal_weak_boundaries(self, g, rdis):
        for above, expect in [(8, 'strong'),   # 0.8>0.7 严格
                              (7, 'normal'),   # 0.7 不>0.7 → normal
                              (5, 'normal'),   # 0.5>0.4 → normal
                              (3, 'weak')]:
            rows = {f'S{i:03}': {'1h': _bull(i) if i < above else _bear(i)}
                    for i in range(10)}
            rdis.store['market:s3_data'] = {'ts': B, 'symbols': rows}
            g._breadth_symbols_cache = [f'S{i:03}' for i in range(10)]
            g._breadth_symbols_ts = B
            breadth, got = g.sample_breadth()
            assert breadth == expect


class TestEmptyPoolFrozen:
    def test_empty_pool_ratio_default_05(self, g, rdis, monkeypatch):
        """S0-4 冻结：池耗尽（空池+刷新失败）→ total=0 → ratio 0.5 → normal（假象）。"""
        def boom(path, params=None):
            raise RuntimeError('net down for test')
        monkeypatch.setattr(g, 'fapi_get', boom)      # 重取也失败 → 池仍空
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': {}}
        breadth, ratio = g.sample_breadth()
        assert breadth == 'normal' and ratio == 0.5        # 缺省中性（total=0 分支）


    def test_pool_fetched_but_s3_empty_also(self, g, rdis, monkeypatch):
        """池刷新成功但 S3 无窗口数据 → 同一稀释路径（全部缺失）；top50 池上限。"""

        class T:
            pass  # placeholder

        def pool(path, params=None):
            return [{'symbol': f'D{i:03}USDT', 'quoteVolume': str(1000 - i)}
                    for i in range(60)]
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': {}}
        monkeypatch.setattr(g, 'fapi_get', pool)
        breadth, ratio = g.sample_breadth()
        # get_breadth_symbols fetch → top50 (D000..D049)，缺 S3 数据 → total=50 → 全不计 above
        assert ratio == 0.0 and breadth == 'weak'

    def test_pool_refresh_fallback_to_old(self, g, rdis, clock, monkeypatch):
        """ticker API 失败 → 沿用旧池（6h 缓存价值）。"""
        def boom(path, params=None):
            raise RuntimeError('net down')
        rdis.store['market:s3_data'] = {'ts': B, 'symbols': {}}
        g._breadth_symbols_cache = ['OLD1']
        g._breadth_symbols_ts = B
        monkeypatch.setattr(g, 'fapi_get', boom)
        syms = g.get_breadth_symbols()
        assert syms == ['OLD1']                              # fallback 老池
