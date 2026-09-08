"""Golden：score_to_fraction + bounded_stop_pct + AtrRiskPositionSizer。"""
import pytest

from strategies.position_models import AtrRiskPositionSizer
from strategies.shared_executor import bounded_stop_pct, score_to_fraction

# ── score_to_fraction ────────────────────────────────────────────────────

class TestScoreToFraction:
    def test_score_0(self):
        assert score_to_fraction(0) == pytest.approx(0.03)

    def test_score_1(self):
        assert score_to_fraction(1) == pytest.approx(0.03)

    def test_score_30(self):
        assert score_to_fraction(30) == pytest.approx(0.045)

    def test_score_59(self):
        assert score_to_fraction(59) == pytest.approx(0.0885)

    def test_score_60(self):
        assert score_to_fraction(60) == pytest.approx(0.09)

    def test_score_84(self):
        assert score_to_fraction(84) == pytest.approx(0.126)

    def test_score_85(self):
        assert score_to_fraction(85) == pytest.approx(0.1275)

    def test_score_100(self):
        assert score_to_fraction(100) == pytest.approx(0.15)

    def test_score_101_clamped_to_max(self):
        assert score_to_fraction(101) == pytest.approx(0.15)

    def test_score_negative_clamped_to_min(self):
        assert score_to_fraction(-5) == pytest.approx(0.03)


# ── bounded_stop_pct ─────────────────────────────────────────────────────

class TestBoundedStopPct:
    def test_base_wins_low_atr(self):
        """atr=0 → 不放大，返回 base。"""
        assert bounded_stop_pct(0.04, 0, 0.08) == pytest.approx(0.04)
        assert bounded_stop_pct(0.06, 0, 0.08) == pytest.approx(0.06)
        assert bounded_stop_pct(0.08, 0, 0.08) == pytest.approx(0.08)

    def test_atr_expansion_normal(self):
        """atr=2 → ATR×2/100=0.04 < base 0.06 → base 胜。"""
        assert bounded_stop_pct(0.06, 2, 0.08) == pytest.approx(0.06)

    def test_atr_expansion_crosses_boundary(self):
        """atr=4 → ATR×2/100=0.08 > base 0.04 → ATR 胜。"""
        assert bounded_stop_pct(0.04, 4, 0.08) == pytest.approx(0.08)

    def test_atr_expansion_high_capped(self):
        """atr=8 → ATR×2/100=0.16 → capped at 0.08。"""
        assert bounded_stop_pct(0.04, 8, 0.08) == pytest.approx(0.08)

    def test_takeover_cap_12(self):
        """takeover cap=12%：atr=9 → ATR×2/100=0.18 → capped at 0.12。"""
        assert bounded_stop_pct(0.04, 9, 0.12) == pytest.approx(0.12)

    def test_takeover_base_wins(self):
        """takeover：base=0.10 > ATR 展开 → base 胜。"""
        assert bounded_stop_pct(0.10, 0, 0.12) == pytest.approx(0.10)

    def test_violent_atr_expansion_capped_8(self):
        """VIOLENT 1.25 extension 但 cap 仍 0.08。"""
        assert bounded_stop_pct(0.04, 5, 0.08) == pytest.approx(0.08)

    def test_atr_zero_returns_base(self):
        """atr=0 → max(base, 0) = base。"""
        assert bounded_stop_pct(0.04, 0, 0.08) == pytest.approx(0.04)


# ── AtrRiskPositionSizer ────────────────────────────────────────────────

class TestSizerDefaults:
    def test_default_params(self):
        s = AtrRiskPositionSizer()
        assert s.pool_budget == pytest.approx(0.80)
        assert s.min_allocation == pytest.approx(0.03)
        assert s.max_allocation == pytest.approx(0.15)
        assert s.risk_per_trade == pytest.approx(0.01)
        assert s.min_notional == pytest.approx(10.0)


class TestSizerBudget:
    def setup_method(self):
        self.s = AtrRiskPositionSizer()

    def test_normal_max_score(self):
        """bal=4000 rem=3200 s=100 lev=3 → 池满 ×15% = 480。"""
        assert self.s.budget(4000, 3200, 100, 3, 0, 0) == pytest.approx(480.0)

    def test_risk_cap_kicks_in(self):
        """stop=4% × lev=3 → 仓位 ≤ 4000×1%/(3×4%) = 333.33。"""
        r = self.s.budget(4000, 3200, 100, 3, 0, 0.04)
        assert r == pytest.approx(4000 * 0.01 / (3 * 0.04), rel=1e-4)

    def test_score_50_halves_allocation(self):
        """score 50 → 分配 7.5% → 3200×0.075 = 240。"""
        assert self.s.budget(4000, 3200, 50, 3, 0, 0) == pytest.approx(240.0)

    def test_score_0_floor_allocation(self):
        """score 0 → 分配 3% 下限 → 3200×0.03 = 96。"""
        assert self.s.budget(4000, 3200, 0, 3, 0, 0) == pytest.approx(96.0)

    def test_high_atr_decay(self):
        """atr=8 → factor max(0.2, 4/8)=0.5 → 480×0.5 = 240。"""
        assert self.s.budget(4000, 3200, 100, 3, 8, 0) == pytest.approx(240.0)

    def test_high_stop_risk_cap(self):
        """stop=10% × lev=3 → 4000×1%/(3×10%) = 133.33。"""
        assert self.s.budget(4000, 3200, 100, 3, 0, 0.10) == pytest.approx(
            4000 * 0.01 / (3 * 0.10), rel=1e-4)

    def test_remaining_zero_min_notional_floor(self):
        """rem=0 → budget=0 → min_notional 10 兜底。"""
        assert self.s.budget(4000, 0, 100, 3, 0, 0) == pytest.approx(10.0)

    def test_small_balance_floor(self):
        """bal=100 rem=80 → 80×0.15=12 → risk cap 100×1%/(3×4%)=8.33 → min(12, 8.33)=8.33 → floor 10。"""
        r = self.s.budget(100, 80, 100, 3, 0, 0.04)
        assert r == pytest.approx(10.0)

    def test_leverage_1_no_risk_cap(self):
        """lev=1 stop=4% → cap=4000×1%/(1×4%)=1000 > 480 → allocation 胜。"""
        assert self.s.budget(4000, 3200, 100, 1, 0, 0.04) == pytest.approx(480.0)

    def test_atr_and_stop_combined(self):
        """atr=8 衰减 ×0.5 → 240；stop=4% cap 333 → min(240, 333)=240。"""
        assert self.s.budget(4000, 3200, 100, 3, 8, 0.04) == pytest.approx(240.0)
