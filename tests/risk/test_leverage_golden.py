"""Golden：leverage_for_score 全矩阵（10 event × 6 score × 2 atr）。"""
import pytest

from strategies.shared_executor import leverage_for_score

EVENT_TYPES = ('PULSE_UP', 'PULSE_DOWN', 'PANIC_SELL', 'TREND_UP', 'TREND_DOWN',
               'VIOLENT_BULLISH', 'VIOLENT_BEARISH', 'PUMP_UP', 'PUMP_DOWN', 'UNKNOWN')

# 探针实测冻结表（event_type → {score: leverage}，atr=0）
LEV_ATR0 = {
    'PULSE_UP':         {0: 2, 59: 2, 60: 3, 84: 3, 85: 5, 100: 5},
    'PULSE_DOWN':       {0: 2, 59: 2, 60: 3, 84: 3, 85: 5, 100: 5},
    'PANIC_SELL':       {0: 2, 59: 2, 60: 3, 84: 3, 85: 5, 100: 5},
    'TREND_UP':         {0: 2, 59: 2, 60: 3, 84: 3, 85: 3, 100: 3},
    'TREND_DOWN':       {0: 2, 59: 2, 60: 3, 84: 3, 85: 3, 100: 3},
    'VIOLENT_BULLISH':  {0: 2, 59: 2, 60: 3, 84: 3, 85: 3, 100: 3},
    'VIOLENT_BEARISH':  {0: 2, 59: 2, 60: 3, 84: 3, 85: 3, 100: 3},
    'PUMP_UP':          {0: 2, 59: 2, 60: 2, 84: 2, 85: 2, 100: 2},
    'PUMP_DOWN':        {0: 2, 59: 2, 60: 2, 84: 2, 85: 2, 100: 2},
    'UNKNOWN':          {0: 2, 59: 2, 60: 3, 84: 3, 85: 3, 100: 3},
}


class TestLeverageMatrix:
    """全 event × score 矩阵（atr=0），来自探针冻结。"""

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_0(self, event_type):
        assert leverage_for_score(event_type, 0) == LEV_ATR0[event_type][0]

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_59(self, event_type):
        assert leverage_for_score(event_type, 59) == LEV_ATR0[event_type][59]

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_60(self, event_type):
        assert leverage_for_score(event_type, 60) == LEV_ATR0[event_type][60]

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_84(self, event_type):
        assert leverage_for_score(event_type, 84) == LEV_ATR0[event_type][84]

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_85(self, event_type):
        assert leverage_for_score(event_type, 85) == LEV_ATR0[event_type][85]

    @pytest.mark.parametrize('event_type', EVENT_TYPES)
    def test_score_100(self, event_type):
        assert leverage_for_score(event_type, 100) == LEV_ATR0[event_type][100]


class TestLeverageAtrBoundary:
    """atr_pct >= 4 时杠杆压到 3（PULSE 家族从 5 降）"""

    def test_pulse_score_100_atr_4(self):
        assert leverage_for_score('PULSE_UP', 100, 4) == 3

    def test_pulse_score_100_atr_3_99(self):
        assert leverage_for_score('PULSE_UP', 100, 3.99) == 5

    def test_pulse_score_100_atr_4_01(self):
        assert leverage_for_score('PULSE_UP', 100, 4.01) == 3

    def test_pulse_score_100_atr_8(self):
        assert leverage_for_score('PULSE_UP', 100, 8) == 3

    def test_trend_score_100_atr_4(self):
        assert leverage_for_score('TREND_UP', 100, 4) == 3

    def test_trend_score_100_atr_8(self):
        assert leverage_for_score('TREND_UP', 100, 8) == 3


class TestLeveragePumpAlwaysTwo:
    """Observed Current Behavior: PUMP_* base=2，即使 score=100/atr=0 也是 2。"""
    def test_pump_up_always_2(self):
        for s in (0, 59, 60, 84, 85, 100):
            assert leverage_for_score('PUMP_UP', s, 0) == 2

    def test_pump_down_always_2(self):
        for s in (0, 59, 60, 84, 85, 100):
            assert leverage_for_score('PUMP_DOWN', s, 0) == 2
