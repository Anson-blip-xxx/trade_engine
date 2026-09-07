"""Golden：contract_score 全量 characterization（公式冻结，非优化）。

期望值全部来自探针实测（P3-01 STEP 1），不来自"理论正确值"。
"""

from strategies.shared_executor import contract_score


def test_baseline_long_no_flow_no_short_ratio():
    """baseline：仅 strength，无任何调整项。"""
    assert contract_score(70, 'TREND_UP', 0, 0, None, 0, 'LONG', None) == 70
    assert contract_score(70, 'TREND_DOWN', 0, 0, None, 0, 'SHORT', None) == 70


def test_flow_aligned_long_boundary():
    """LONG：taker>=0.52 → +5；0.51 → -5。"""
    assert contract_score(70, 'TREND_UP', 0, 0, 0.52, 0, 'LONG', None) == 75
    assert contract_score(70, 'TREND_UP', 0, 0, 0.51, 0, 'LONG', None) == 65


def test_flow_aligned_short_boundary():
    """SHORT：taker<=0.48 → +5；0.49 → -5。"""
    assert contract_score(70, 'TREND_DOWN', 0, 0, 0.48, 0, 'SHORT', None) == 75
    assert contract_score(70, 'TREND_DOWN', 0, 0, 0.49, 0, 'SHORT', None) == 65


def test_short_ratio_long_bullish():
    """LONG：short_ratio>=0.60 → +8（空头拥挤利多）。"""
    assert contract_score(70, 'TREND_UP', 0, 0, None, 0, 'LONG', 0.60) == 78


def test_short_ratio_long_bearish():
    """LONG：short_ratio<=0.40 → -5。"""
    assert contract_score(70, 'TREND_UP', 0, 0, None, 0, 'LONG', 0.40) == 65


def test_short_ratio_long_neutral():
    assert contract_score(70, 'TREND_UP', 0, 0, None, 0, 'LONG', 0.50) == 70


def test_short_ratio_short_bullish():
    """SHORT：short_ratio<=0.40 → +5。"""
    assert contract_score(70, 'TREND_DOWN', 0, 0, None, 0, 'SHORT', 0.40) == 75


def test_short_ratio_short_bearish():
    """SHORT：short_ratio>=0.60 → -8。"""
    assert contract_score(70, 'TREND_DOWN', 0, 0, None, 0, 'SHORT', 0.60) == 62


def test_high_atr_penalty_and_cap():
    """ATR>4 罚分 (atr-4)*3，上限 15：atr 5→-3 / 9→-15 / 20→-15（封顶）。"""
    assert contract_score(70, 'TREND_UP', 5, 0, None, 0, 'LONG', None) == 67
    assert contract_score(70, 'TREND_UP', 9, 0, None, 0, 'LONG', None) == 55
    assert contract_score(70, 'TREND_UP', 20, 0, None, 0, 'LONG', None) == 55


def test_extension_penalty_and_cap():
    """extension>0 罚分 ×4，上限 15：1.0→-4 / 5.0→-15（封顶）。"""
    assert contract_score(70, 'TREND_UP', 0, 1.0, None, 0, 'LONG', None) == 66
    assert contract_score(70, 'TREND_UP', 0, 5.0, None, 0, 'LONG', None) == 55


def test_age_penalty_and_cap():
    """age>0 罚分 /30，上限 10：30s→-1 / 600s→-10（封顶）。"""
    assert contract_score(70, 'TREND_UP', 0, 0, None, 30, 'LONG', None) == 69
    assert contract_score(70, 'TREND_UP', 0, 0, None, 600, 'LONG', None) == 60


def test_multiple_penalties_stack():
    """多罚分叠加：70 - 4(ext1.0) - 5(age150) = 61。"""
    assert contract_score(70, 'TREND_UP', 0, 1.0, None, 150, 'LONG', None) == 61


def test_score_lower_clamp_zero():
    """下限 0：10 - 15(ext) - 15(atr) - 10(age) → 负值 clamp 0。"""
    assert contract_score(10, 'TREND_UP', 9, 5.0, None, 600, 'LONG', None) == 0


def test_score_upper_clamp_100():
    """上限 100：99 + 5(flow) + 8(short_ratio) → clamp 100。"""
    assert contract_score(99, 'TREND_UP', 0, 0, 0.60, 0, 'LONG', 0.60) == 100


def test_event_type_does_not_affect_score():
    """event_type 仅作签名参数，不影响计算（当前行为）。"""
    assert (contract_score(70, 'TREND_UP', 0, 0, None, 0, 'LONG', None)
            == contract_score(70, 'PANIC_SELL', 0, 0, None, 0, 'LONG', None))
