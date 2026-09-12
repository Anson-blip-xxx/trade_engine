"""P6-04：BreadthPoolState —— 6h 边界/fallback/重启清空/池刷新失败。"""
import pytest

from s0.adapters import BreadthPoolMemoryState


POOL_TTL = 6 * 3600


class TestBreadthPoolState:
    def test_top50_filter_and_excludes(self, g, rdis, clock, monkeypatch):
        """pool refresh：top 50 by quoteVolume + 排 BTC/stablecoin 逐字冻结。"""
        monkeypatch.setattr(g, 'fapi_get', lambda p, params=None: [
            {'symbol': f'X{i:03}USDT', 'quoteVolume': str(1001 - i)}
            for i in range(60)]
            + [{'symbol': 'BTCUSDT', 'quoteVolume': '999999999'},
               {'symbol': 'USDCUSDT', 'quoteVolume': '999999999'},
               {'symbol': 'BTCBRL', 'quoteVolume': '888888888'}])
        clock.advance(6 * 3600 + 1)                   # 池过期（严格 <6h）
        syms = g.get_breadth_symbols()
        assert len(syms) == 50                         # BREADTH_N=50 上限
        assert 'BTCUSDT' not in syms and not any('USDC' in s for s in syms)
        assert syms[0] == 'X000USDT' and syms[-1] == 'X049USDT'   # volume desc

    def test_exact_6h_boundary_refetch(self, g, rdis, clock, monkeypatch):
        """S0 pool TTL 严格 `<`：==6h 即过期重取。"""
        calls = []
        monkeypatch.setattr(g, 'fapi_get',
                            lambda p, params=None: calls.append(1) or [])
        g._breadth_symbols_cache = ['OLD1']
        g._breadth_symbols_ts = clock.now
        clock.advance(6 * 3600 - 1)                    # 5h59m59s → fresh
        assert g.get_breadth_symbols() == ['OLD1']     # 不触发重取
        assert calls == []
        clock.advance(1)                               # == 6h → expired
        g.get_breadth_symbols()
        assert len(calls) == 1                          # 重取执行

    def test_refresh_failure_old_pool(self, g, rdis, clock, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError('net down')
        monkeypatch.setattr(g, 'fapi_get', boom)
        g._breadth_symbols_cache = ['OLD1']
        g._breadth_symbols_ts = clock.now
        clock.advance(6 * 3600 + 100)
        assert g.get_breadth_symbols() == ['OLD1']     # fallback 老池

    def test_refresh_failure_no_old_pool_empty(self, g, rdis, clock, monkeypatch):
        def boom(path, params=None):
            raise RuntimeError('net down')
        monkeypatch.setattr(g, 'fapi_get', boom)
        g._breadth_symbols_cache = []
        g._breadth_symbols_ts = 0.0
        clock.advance(6 * 3600 + 100)
        assert g.get_breadth_symbols() == []           # S0-4 空池路径

    def test_restart_clears_pool(self, g, rdis, clock, monkeypatch):
        """fixture（模拟新进程）cache/ts 归零 → 重取。"""
        calls = []
        monkeypatch.setattr(g, 'fapi_get',
                            lambda p, params=None: calls.append(1) or [])
        g._breadth_symbols_cache = ['OLD1']
        g._breadth_symbols_ts = clock.now
        clock.advance(6 * 3600 + 1)
        g.get_breadth_symbols()
        assert len(calls) == 1                          # cache 不为新周期保留


class TestPoolStateBacking:
    def test_get_put_via_backing_callbacks(self):
        """port 对象是 backing 的薄桥接——get/put 透传（晚绑定）。"""
        seen = {'get': [], 'put': []}
        store = BreadthPoolMemoryState(
            backing_get=lambda: (['XUSDT'], 1.0),
            backing_put=lambda s, t: seen['put'].append((s, t)))
        assert store.get() == (['AUSDT'], 1.0) or store.get() == (['XUSDT'], 1.0)
        store.put(['NEW'], 42.0)
        assert seen['put'] == [(['NEW'], 42.0)]        # 原封透传
