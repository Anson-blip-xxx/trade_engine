"""Golden：calc_position_qty 全场景（IO mocked，Risk 公式真实执行）。"""
import pytest

from strategies import shared_executor as se


@pytest.fixture
def qty_env(monkeypatch):
    """calc_position_qty 的 IO 隔离：余额可控，日志静默。"""
    state = {'balance': 4000.0, 'used': 0.0}
    monkeypatch.setattr(se, '_get_balance', lambda: state['balance'])
    monkeypatch.setattr(se, '_calc_used_margin', lambda s: state['used'])
    monkeypatch.setattr(se, '_log', lambda *a, **k: None)
    return state


def _calc(state, **kw):
    base = {'name': 'S6', 'state': {}, 'symbol': 'XUSDT', 'price': 100.0,
            'event_type': 'TREND_UP', 'strength': 100, 'leverage': 3,
            'atr_pct': 2, 'stop_pct': 0}
    base.update(kw)
    return se.calc_position_qty(**base)


class TestCalcQtyNormal:
    def test_case_a_normal(self, qty_env):
        """bal=4000, used=0, s=100, lev=3 → pool 3200 × 15% = 480 → qty = 480/100×3 = 14.4。"""
        r = _calc(qty_env)
        assert r == pytest.approx(14.4)

    def test_case_b_used_margin(self, qty_env):
        """used=800 → remaining 2400 → 2400×0.15 = 360 → qty = 10.8。"""
        qty_env['used'] = 800.0
        r = _calc(qty_env)
        assert r == pytest.approx(10.8)

    def test_case_c_remaining_zero(self, qty_env):
        """used = pool → remaining = 0 → sizer floor → 10 USDT → qty = 10/100×3 = 0.3。"""
        qty_env['used'] = 3200.0  # pool 全用完
        r = _calc(qty_env)
        # sizer: min(3200, 0) = 0 → 0×0.15=0 → max(0, 10) = 10 → qty = 10/100×3 = 0.3
        assert r == pytest.approx(0.3)


class TestCalcQtyScore:
    def test_case_d_low_score(self, qty_env):
        """score=20 → 分配 3% 下限 → 96 USDT → qty = 96/100×3 = 2.88。"""
        r = _calc(qty_env, strength=20)
        assert r == pytest.approx(2.88)

    def test_case_e_high_score(self, qty_env):
        """score=100 → 分配 15% 上限 → 480 USDT → qty = 14.4。"""
        r = _calc(qty_env, strength=100)
        assert r == pytest.approx(14.4)


class TestCalcQtyAtrStop:
    def test_case_f_high_atr(self, qty_env):
        """atr=8 → 衰减 factor max(0.2, 4/8)=0.5 → 240 USDT → qty = 240/100×3 = 7.2。"""
        r = _calc(qty_env, atr_pct=8)
        assert r == pytest.approx(7.2)

    def test_case_g_high_stop_risk(self, qty_env):
        """stop=8% × lev=3 → risk cap 4000×1%/(3×8%)=16.67 USDT → qty = 16.67/100×3 = 0.50。"""
        r = _calc(qty_env, stop_pct=0.08)
        assert r == pytest.approx(
            4000 * 0.01 / (3 * 0.08) / 100 * 3, rel=1e-4)


class TestCalcQtyEdge:
    def test_case_h_min_notional(self, qty_env):
        """极小 remaining → min_notional 10 兜底。"""
        qty_env['used'] = 3199.0  # remaining = 3200 - 3199 = 1
        r = _calc(qty_env)
        # sizer: min(3200, 1)=1 → 1×0.15=0.15 → max(0.15, 10) = 10 → qty = 10/100×3 = 0.3
        assert r == pytest.approx(0.3)

    def test_case_i_lev_1(self, qty_env):
        """lev=1 → qty = 480/100×1 = 4.8。"""
        r = _calc(qty_env, leverage=1)
        assert r == pytest.approx(4.8)

    def test_case_j_price_small(self, qty_env):
        """price=0.01 → qty = 480/0.01×3 = 144000。"""
        r = _calc(qty_env, price=0.01)
        assert r == pytest.approx(144000.0)
