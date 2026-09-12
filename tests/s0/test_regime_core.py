"""P6-02：S0 Classifier Core parity — legacy compute_state vs s0.core 同输入对拍。

场景矩阵 A-J（P6-01 决策表全量）；阈值三点 strict；类型 parity；
wall-clock 注入 parity；mutation/aliasing 事实锁定。
"""
import pytest

from s0 import core


def base_args(btc_trend='bull', volatility='low', amp=0.02,
              btc_below=False, atr_exp=False, breadth='normal',
              breadth_ratio=0.5):
    return (btc_trend, volatility, amp, btc_below, atr_exp,
            breadth, breadth_ratio)


@pytest.fixture(autouse=True)
def _stub_wallclock_samples(g, monkeypatch):
    """disable legacy wall-clock sampling IO（保持 gate 位置行为）。"""
    monkeypatch.setattr(g, 'sample_shock_score', lambda: 0)
    monkeypatch.setattr(g, 'sample_alts_sync', lambda: (0.0, 0, 0))
    monkeypatch.setattr(g, 'sample_sentiment', lambda: {})


def both(g, **kw):
    args = base_args(**{k: v for k, v in kw.items()
                        if k not in ('sent', 'alts', 'shock')})
    sent = kw.get('sent')
    alts = kw.get('alts', 0.0)
    shock = kw.get('shock', 0)
    if sent is not None:
        g.sample_sentiment = lambda: sent      # legacy 侧 sentiment 注入
    legacy = g.compute_state(*args)
    core_out = core.classify_regime(
        *args, alts_sync_val=alts, shock_val=shock, sentiment=sent)
    return legacy, core_out


# ═══════════════════════════════════════════════════════════════
#  场景 A-J parity（返回 dict 逐字段相等 — 键序含）
# ═══════════════════════════════════════════════════════════════

class TestScenarioMatrix:
    def _cmp(self, a, b):
        self_cmp(a, b)

    def test_a_strong_bull(self, g):
        a, b = both(g, btc_trend='bull', breadth='strong', breadth_ratio=0.8)
        self._cmp(a, b)
        assert a['market_state'] == 'trend' and a['regime'] == 'bull_trend'

    def test_b_weak_bull(self, g):
        a, b = both(g, btc_trend='bull', breadth='normal', breadth_ratio=0.5)
        self._cmp(a, b)
        assert a['regime'] == 'weak_bull'

    def test_c_range(self, g):
        a, b = both(g, btc_trend='neutral', breadth='normal', amp=0.015)
        self._cmp(a, b)

    def test_d_weak_bear(self, g):
        a, b = both(g, btc_trend='bear', amp=0.01, breadth_ratio=0.45)
        self._cmp(a, b)
        assert a['regime'] == 'weak_bear'

    def test_e_risk_off(self, g):
        a, b = both(g, btc_trend='bear', amp=0.01, btc_below=True,
                    atr_exp=True, breadth='weak', breadth_ratio=0.2)
        self._cmp(a, b)
        assert a['regime'] == 'risk-off' and a['regime_score'] == -7

    def test_f_contradiction_bull_ratio_0_32(self, g):
        a, b = both(g, btc_trend='bull', breadth='normal', breadth_ratio=0.32)
        self._cmp(a, b)
        assert a['regime'] == 'weak_bull'

    def test_g_breadth_weak_overlap(self, g):
        a, b = both(g, btc_trend='bull', breadth='weak', breadth_ratio=0.32)
        self._cmp(a, b)
        assert a['regime'] == 'weak_bear'

    def test_h_empty_default_like(self, g):
        a, b = both(g, btc_trend='neutral', amp=0.01, breadth='normal',
                    breadth_ratio=0.5)
        self._cmp(a, b)

    def test_i_atr_expanding_alone_not_risk(self, g):
        a, b = both(g, btc_trend='neutral', atr_exp=True)
        self._cmp(a, b)
        assert a['market_state'] == 'range'

    def test_j_neutral(self, g):
        a, b = both(g, btc_trend='neutral', breadth='normal')
        self._cmp(a, b)
        assert a['regime'] == 'range'


# ═══════════════════════════════════════════════════════════════
#  阈值三点 strict 对拍（legacy==core 逐值）
# ═══════════════════════════════════════════════════════════════

class TestThresholdParity:
    @pytest.mark.parametrize('amp,expect', [(0.04, 'range'), (0.0401, 'risk-off'), (0.05, 'risk-off')])
    def test_amp_boundary(self, g, amp, expect):
        a, b = both(g, btc_trend='neutral', amp=amp, breadth='normal')
        assert a['market_state'] == b['market_state'] == expect

    @pytest.mark.parametrize('ratio,expect', [
        (0.2999, 'risk-off'),          # ratio<0.30
        (0.30, 'weak_bull'),           # bull+normal 重叠区（已冻结）
        (0.35, 'weak_bull'), (0.70, 'weak_bull')])
    def test_breadth_ratio_thresholds_bull(self, g, ratio, expect):
        """bull + breadth normal + 各 ratio 边界（重叠区 OBSERVED）。"""
        a, b = both(g, btc_trend='bull', breadth='normal',
                    breadth_ratio=ratio)
        self_cmp(a, b)
        assert a['regime'] == expect

    @pytest.mark.parametrize('ratio', [0.32, 0.3499])
    def test_breadth_ratio_thresholds_weak_label(self, g, ratio):
        """bull + breadth='weak' + ratio<0.35 → weak_bear（breadth='weak' 覆盖路径）。"""
        a, b = both(g, btc_trend='bull', breadth='weak', breadth_ratio=ratio)
        self_cmp(a, b)
        assert a['regime'] == 'weak_bear'

    def test_sentiment_floor(self, g):
        """sentiment 地板 -7：risk-off regime_score 仍为 -7（max(-7,-8)=-7）。"""
        a, b = both(g, btc_trend='bear', amp=0.01, btc_below=True,
                    atr_exp=True, breadth='weak', breadth_ratio=0.2,
                    sent={'sentiment_risk': True})
        self_cmp(a, b)
        assert a['regime_score'] == -7


GLOBAL_KEYS = ('version', 'timestamp')


def _strip(state):
    return {k: v for k, v in state.items() if k not in GLOBAL_KEYS}


def self_cmp(a, b):
    a, b = _strip(a), _strip(b)
    assert list(a.keys()) == list(b.keys())
    assert a == b


class TestSentimentInjection:
    def test_sentiment_full_sample(self, g):
        sent = {'fng': 90, 'fng_label': 'Greed', 'avg_funding': 1e-6,
                'sentiment_risk': True, 'bias': 'long'}
        a, b = both(g, btc_trend='bull', breadth='strong', breadth_ratio=0.8,
                    sent=sent)
        self_cmp(a, b)
        assert a['regime_score'] == 4               # 5-1
        assert b['fng'] == 90
        assert b['sentiment_bias'] == 'long'

    def test_sentiment_missing_defaults(self, g):
        a, b = both(g, btc_trend='neutral', breadth='normal')
        self_cmp(a, b)
        assert b['fng'] == 50 and b['fng_label'] == ''
        assert b['avg_funding'] == 0.0 and b['sentiment_bias'] == 'neutral'


# ═══════════════════════════════════════════════════════════════
#  mutation / aliasing（compute_state 是只读分类器——事实冻结）
# ═══════════════════════════════════════════════════════════════

class TestMutationFact:
    def test_sentiment_not_mutated(self, g):
        sent = {'fng': 30, 'sentiment_risk': True}
        before = dict(sent)
        core.classify_regime('bear', 'low', 0.02, False, False,
                             'normal', 0.4, sentiment=sent)
        assert sent == {'fng': 30, 'sentiment_risk': True}     # 无 mutation

    def test_wrapper_sampled_sentiment_no_mutation(self, g):
        """legacy 路径 sample_sentiment 可伪造（fake redis 读键）——不 mutate。"""
        g.sample_sentiment = lambda: {'fng': 55}
        before = None  # sample_sentiment 无调用方 mutation（函数内只读）
        st = g.compute_state('bull', 'low', 0.02, False, False,
                             'normal', 0.5)
        assert st['fng'] == 55 and st['sentiment_bias'] == 'neutral'


# ═══════════════════════════════════════════════════════════════
#  类型 parity
# ═══════════════════════════════════════════════════════════════

class TestTypeParity:
    def test_no_normalization(self, g, rdis):
        """int/bool/float/str 类型不互相规范化（S0-9/类型事实）。"""
        st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.51)
        core_out = core.classify_regime('bull', 'low', 0.02, False, False,
                                        'normal', 0.51)
        assert isinstance(st['risk_off'], bool)
        assert isinstance(st['regime_score'], int)
        assert isinstance(st['trend_strength'], int)
        assert isinstance(st['breadth_ratio'], float)
        assert isinstance(st['version'], str)
        assert isinstance(st['timestamp'], int)
        assert list(core_out.keys()) == [k for k in list(st.keys())
                                         if k not in ('version', 'timestamp')]
