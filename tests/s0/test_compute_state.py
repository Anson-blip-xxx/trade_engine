"""P6-01：compute_state decision table golden（7 行 + 阈值三点 + payload 契约）。"""
import pytest

from conftest import sym_row, seed_s3

BASE = 1_700_000_000


def base_args(btc_trend='neutral', volatility='normal', amp=0.02,
              btc_below=False, atr_exp=False, breadth='normal',
              breadth_ratio=0.5):
    return (btc_trend, volatility, amp, btc_below, atr_exp,
            breadth, breadth_ratio)


# ═══════════════════════════════════════════════════════════════
#  Decision Table（P6-00 §13 的 7 行全冻结）
# ═══════════════════════════════════════════════════════════════

class TestDecisionTable:
    def test_a_btc_below_ema60_atr_expand_risk_off(self, g):
        st = g.compute_state(*base_args(btc_trend='bear', amp=0.02,
                                        btc_below=True, atr_exp=True,
                                        breadth_ratio=0.5))
        assert st['market_state'] == 'risk-off'
        assert st['regime'] == 'risk-off'
        assert st['regime_score'] == -7 and st['trend_strength'] == 0
        assert st['s6_allowed'] is False and st['s8_allowed'] is False \
            and st['s7_allowed'] is False

    def test_b_amp_over_0_04_independent(self, g):
        st = g.compute_state(*base_args(btc_trend='bull', amp=0.041,
                                        breadth='strong'))
        assert st['market_state'] == 'risk-off'      # amp 单独触发
        assert st['regime'] == 'risk-off'

    def test_c_breadth_lt_0_30_independent(self, g):
        st = g.compute_state(*base_args(amp=0.01, breadth='weak',
                                        breadth_ratio=0.29))
        assert st['market_state'] == 'risk-off'

    def test_d_bull_trend_strong(self, g):
        st = g.compute_state(*base_args(btc_trend='bull', breadth='strong',
                                        breadth_ratio=0.8))
        assert st['market_state'] == 'trend'
        assert st['regime'] == 'bull_trend' and st['regime_score'] == 5
        assert st['trend_strength'] == 85
        assert st['s6_allowed'] is True and st['s8_allowed'] is False \
            and st['s7_allowed'] is False

    def test_e_bull_medium_breadth_weak_bull(self, g):
        st = g.compute_state(*base_args(btc_trend='bull', breadth='normal',
                                        breadth_ratio=0.68))
        assert st['market_state'] == 'range'
        assert st['regime'] == 'weak_bull' and st['regime_score'] == 3
        assert st['s8_allowed'] is True   # 空头允许（非 bull_trend）

    def test_f_contradiction_zone_bull_ratio_0_32_still_weak_bull(self, g):
        """P6-00 冻结：bull + breadth normal + ratio 0.32（<0.35 未破 0.30）
        → 仍然 weak_bull（规则重叠区 OBSERVED；breadth='weak' 时则落 weak_bear）。"""
        st = g.compute_state(*base_args(btc_trend='bull', breadth='normal',
                                        breadth_ratio=0.32))
        assert st['market_state'] == 'range'
        assert st['regime'] == 'weak_bull'            # 规则重叠 OBSERVED
        st2 = g.compute_state(*base_args(btc_trend='bull', breadth='weak',
                                         breadth_ratio=0.32))
        assert st2['regime'] == 'weak_bear'           # breadth='weak' 分支路径不同

    def test_g_bear_or_low_breadth_weak_bear(self, g):
        st = g.compute_state(*base_args(btc_trend='bear', amp=0.01,
                                        breadth='normal', breadth_ratio=0.45))
        assert st['market_state'] == 'range'
        assert st['regime'] == 'weak_bear' and st['regime_score'] == -3
        assert st['trend_strength'] == 25

    def test_g2_breadth_0_35_boundary_alone_weak_bear(self, g):
        st = g.compute_state(*base_args(btc_trend='neutral', amp=0.01,
                                        breadth='normal',
                                        breadth_ratio=0.34))
        assert st['regime'] == 'weak_bear'            # ratio<0.35 单独触发
        st2 = g.compute_state(*base_args(btc_trend='neutral', amp=0.01,
                                         breadth_ratio=0.35))
        assert st2['regime'] == 'range'               # 边界相等 → range

    def test_h_neutral_range(self, g):
        st = g.compute_state(*base_args(btc_trend='neutral', amp=0.015))
        assert st['market_state'] == 'range'
        assert st['regime'] == 'range' and st['regime_score'] == 0
        assert st['trend_strength'] == 50
        assert st['s7_allowed'] is True and st['s8_allowed'] is True


# ═══════════════════════════════════════════════════════════════
#  risk-off 三条件阈值边界（三点式）
# ═══════════════════════════════════════════════════════════════

class TestRiskOffThresholds:
    def test_amp_strict_gt(self, g):
        assert g.compute_state(*base_args(amp=0.04))['market_state'] == 'range'
        assert g.compute_state(*base_args(amp=0.0401))['market_state'] == 'risk-off'

    def test_breadth_strict_lt(self, g):
        assert g.compute_state(*base_args(breadth_ratio=0.30))['market_state'] == 'range'
        assert g.compute_state(*base_args(breadth_ratio=0.299))['market_state'] == 'risk-off'

    def test_btc_below_and_atr_expanding_requires_both(self, g):
        assert g.compute_state(*base_args(btc_below=True,
                                          atr_exp=False))['market_state'] == 'range'
        assert g.compute_state(*base_args(btc_below=False,
                                          atr_exp=True))['market_state'] == 'range'

    def test_combined_or(self, g):
        st = g.compute_state(*base_args(btc_below=True, atr_exp=True,
                                        amp=0.05, breadth_ratio=0.2))
        assert st['market_state'] == 'risk-off'
        assert st['risk_off'] is True


class TestRegimeThresholds:
    def test_breadth_strong_boundary(self, g):
        a = g.compute_state(*base_args(btc_trend='bull', breadth='strong',
                                       breadth_ratio=0.70))
        assert a['regime'] == 'bull_trend'
        b = g.compute_state(*base_args(btc_trend='bull', breadth='normal',
                                       breadth_ratio=0.699))
        assert b['regime'] == 'weak_bull'             # strong 分档由 breadth 字段而非 ratio

    def test_bull_needs_strict_ema(self, g, rdis):
        """price==ema20 边界 → 不满足 bull 条件。"""
        rdis.store['market:s3_data'] = {'ts': BASE, 'symbols': {
            'BTCUSDT': {'4h': sym_row('BTCUSDT', close=100.0, ema20=100.0),
                        '1h': sym_row('BTCUSDT', 100.0),
                        '24h': sym_row('BTCUSDT', 100.0),
                        '15m': sym_row('BTCUSDT', 100.0)}}}
        trend, vol, amp, below, exp = g.sample_btc()
        assert trend == 'neutral'                      # price==ema20 → 非 bull/bear
        assert vol == 'normal'                         # amp=0.02 ∈ (0.015, 0.03) → normal 档
        assert amp == pytest.approx(0.02)


# ═══════════════════════════════════════════════════════════════
#  payload 契约（types/shape/version）
# ═══════════════════════════════════════════════════════════════

class TestPayloadContract:
    REQUIRED = ('version', 'timestamp', 'market_state', 'btc_trend', 'breadth',
                'breadth_ratio', 'volatility', 'risk_off', 'regime',
                'regime_score', 'trend_strength', 's6_allowed', 's7_allowed',
                's8_allowed', 'fng', 'fng_label', 'avg_funding',
                'sentiment_risk', 'sentiment_bias', 'alts_sync',
                'shock_score')

    def test_all_required_fields_and_types(self, g, rdis):
        st = g.compute_state(*base_args())
        for k in self.REQUIRED:
            assert k in st
        assert st['version'] == '1.1.0'
        assert st['timestamp'] == BASE     # fake clock int()
        assert isinstance(st['breadth_ratio'], float)
        assert isinstance(st['risk_off'], bool)
        assert st['alts_sync'] == 0.0 or isinstance(st['alts_sync'], (int, float))
        assert st['shock_score'] == 0 or isinstance(st['shock_score'], int)

    def test_no_market_mode_key_in_payload(self, g, rdis):
        """S0-1 的 producer 侧确认：payload 无 market_mode。"""
        st = g.compute_state(*base_args())
        assert 'market_mode' not in st
        assert st['market_state'] == 'range'

    def test_sentiment_risk_drops_score_to_floor(self, g, rdis):
        rdis.store['market:sentiment'] = {'sentiment_risk': True,
                                          'fng': 90, 'fng_label': 'Extreme',
                                          'avg_funding': 0.001,
                                          'bias': 'long'}
        st = g.compute_state(*base_args(btc_trend='bull', breadth='strong',
                                        breadth_ratio=0.8))
        assert st['regime_score'] == 4              # 5-1（地板 -7 未触）
        assert st['sentiment_risk'] is True and st['fng'] == 90
