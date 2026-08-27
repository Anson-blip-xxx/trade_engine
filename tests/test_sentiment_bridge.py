"""sentiment_bridge 回归测试：情绪风险叠加逻辑。"""
import importlib.util
import sys
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

_spec = importlib.util.spec_from_file_location('sentiment_bridge', _BASE / 'services' / 'sentiment_bridge.py')
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)


def test_compute_sentiment_extreme_greed():
    r = sb.compute_sentiment(90, 0.00001)
    assert r['sentiment_risk'] is True
    assert r['bias'] == 'greed'


def test_compute_sentiment_extreme_fear():
    r = sb.compute_sentiment(10, 0.00001)
    assert r['sentiment_risk'] is True
    assert r['bias'] == 'fear'


def test_compute_sentiment_crowded_funding_long():
    r = sb.compute_sentiment(50, 0.0006)
    assert r['sentiment_risk'] is True
    assert r['bias'] == 'neutral'


def test_compute_sentiment_crowded_funding_short():
    r = sb.compute_sentiment(50, -0.0006)
    assert r['sentiment_risk'] is True


def test_compute_sentiment_neutral():
    r = sb.compute_sentiment(50, 0.00001)
    assert r['sentiment_risk'] is False
    assert r['bias'] == 'neutral'


def test_funding_weighted_by_volume(monkeypatch):
    class _Resp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def fake_get(url, timeout=10):
        if '24hr' in url:
            return _Resp([
                {'symbol': 'BTCUSDT', 'quoteVolume': '900'},
                {'symbol': 'ETHUSDT', 'quoteVolume': '100'},
                {'symbol': 'SHITUSDT', 'quoteVolume': '0'},
            ])
        if 'premiumIndex' in url:
            return _Resp([
                {'symbol': 'BTCUSDT', 'lastFundingRate': '0.0003'},
                {'symbol': 'ETHUSDT', 'lastFundingRate': '-0.0001'},
                {'symbol': 'SHITUSDT', 'lastFundingRate': '0.0100'},
            ])
        raise AssertionError(url)

    monkeypatch.setattr(sb.requests, 'get', fake_get)
    fund = sb.fetch_funding()
    # 加权 = (0.0003*900 + (-0.0001)*100 + 0) / (900+100) = 0.00026
    assert fund['avg_funding'] == pytest.approx(0.00026, abs=1e-8)
    assert fund['funding_sample'] == 3
