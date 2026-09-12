"""P6-01：wall-clock mod 门（alts_sync/shock_score）+ S0 决策集成。"""
import pytest

from conftest import BASE_TS, sym_row


def _state(g, fake_now_mod, **kw):
    return g.compute_state(kw.get('btc_trend', 'bull'),
                           kw.get('volatility', 'low'), kw.get('amp', 0.02),
                           kw.get('btc_below', False), kw.get('atr_exp', False),
                           kw.get('breadth', 'normal'),
                           kw.get('breadth_ratio', 0.5))


class TestWallClockMod:
    def test_alts_sync_off_period(self, g, rdis, clock):
        """墙钟 ts%1800 不 <30 → alts_sync 直接 0.0（不采样，
        determinism 限制 S0-3）。"""
        clock.advance(((BASE_TS % 1800) + 100))          # 保证 %1800 > 30
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        assert st['alts_sync'] == 0.0                    # 不在窗口 → 恒 0

    def test_alts_sync_window(self, g, rdis, clock, monkeypatch):
        """ts%1800<30 → 采样分支（fake clock）。"""
        seed = {'ts': BASE_TS, 'symbols': {}}
        rdis.store['market:s3_data'] = seed
        clock.advance(1800 - (BASE_TS % 1800))   # %1800 → 0 <30（进入采样窗）
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        # s3 空 → sample_alts_sync → (0.5,0,0) 但 btc chg==0 → (0.5,0,0)/零
        assert st['alts_sync'] == 0.5     # alts 空数据时的真实缺省（0.5 计数）

    def test_shock_score_needs_ticker(self, g, rdis, clock, monkeypatch):
        """%60<30 → sample_shock_score → fapi_get 失败 → 0（吞错）。"""
        class Boom:
            def __init__(self, *a, **k):
                raise RuntimeError('net down')
        monkeypatch.setattr('services.s0.s0_market_guard.requests.get', Boom)
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        assert st['shock_score'] == 0                  # not improved (KNOWN)

    def test_shock_score_extreme_moves(self, g, rdis, clock, monkeypatch):
        """15% ± 极大于 5 币 → ext//2+2 与 min(extreme//2,4) —— 冻结公式。"""
        rows = [{'priceChangePercent': 20.0} for _ in range(10)]
        monkeypatch.setattr('services.s0.s0_market_guard.fapi_get',
                            lambda p, params=None: rows)
        clock.advance(60 - (clock.now % 60))           # ensure %60<30
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        assert st['shock_score'] == 6                  # min(10//2,4)=4 + 2 = 6


class TestS3DependencyFrozen:
    def test_s0_reads_s3_only_no_binance_fallback(self, g, rdis, monkeypatch):
        """S0 缺 S3 数据 → 一切归 0 / neutral；不尝试 Binance 直接取 Forex。"""
        # market:s3_data 空（fixture）——调用 sample_btc；无中转 REST 调用
        res = g.sample_btc()
        assert res[0] == 'neutral'                       # ema 全 0 → neutral（缺数据降级）
        assert res[3] is False and res[4] is False
