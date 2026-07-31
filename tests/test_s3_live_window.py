"""s3 窗口实时性回归测试：进行中 1m K 线合并进窗口计算。"""
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))

import strategies.s3_orderflow as s3


def _bar(t, c, h=None, l=None, v=10):
    return {'t': t, 'o': c, 'h': h or c, 'l': l or c, 'c': c, 'v': v}


def test_merged_klines_prepends_current_bar(monkeypatch):
    """进行中 K 线比最后一根已收盘新 → 合并到最前。"""
    closed = [_bar(100, 1.0), _bar(99, 0.99), _bar(98, 0.98)]
    cur = _bar(101, 1.05, h=1.06, l=0.99)
    monkeypatch.setattr(s3, '_symbol_klines', {'BTCUSDT': closed})
    monkeypatch.setattr(s3, '_current_kline', {'BTCUSDT': cur})

    merged = s3._merged_klines('BTCUSDT')
    assert len(merged) == 4
    assert merged[0]['t'] == 101  # 进行中 K 线在最前（最新）
    assert merged[1]['t'] == 100


def test_merged_klines_without_current_bar(monkeypatch):
    """无进行中 K 线 → 只返回已收盘。"""
    closed = [_bar(100, 1.0), _bar(99, 0.99)]
    monkeypatch.setattr(s3, '_symbol_klines', {'BTCUSDT': closed})
    monkeypatch.setattr(s3, '_current_kline', {})

    assert len(s3._merged_klines('BTCUSDT')) == 2


def test_merged_klines_current_stale(monkeypatch):
    """进行中 K 线时间戳不新于已收盘 → 不合并（避免重复）。"""
    closed = [_bar(100, 1.0)]
    monkeypatch.setattr(s3, '_symbol_klines', {'BTCUSDT': closed})
    monkeypatch.setattr(s3, '_current_kline', {'BTCUSDT': _bar(100, 1.01)})

    assert len(s3._merged_klines('BTCUSDT')) == 1


def test_window_reflects_live_close():
    """合并进行中 K 线后，窗口 close/chg 反映最新实时价格。"""
    candles = [_bar(t, 1.0 + i * 0.01) for i, t in enumerate(range(200, 186, -1))]
    live = _bar(201, 1.25, h=1.26, l=1.00)
    merged = [live] + candles

    w = s3.compute_window_data(merged, 15, 'BTCUSDT')
    assert w['close'] == 1.25  # 最新实时价
    assert w['chg'] > 0        # 窗口内上涨
    assert w['high'] == 1.26   # 包含进行中 K 线的高点


def test_compute_and_detect_uses_live_bar(monkeypatch):
    """compute_and_detect 快照合并进行中 K 线（产出窗口含实时收盘价）。"""
    import time
    monkeypatch.setattr(s3, '_symbol_klines', {'AAAUSDT': [_bar(t, 1.0) for t in range(200, 185, -1)]})
    monkeypatch.setattr(s3, '_current_kline', {'AAAUSDT': _bar(201, 1.5, h=1.6, l=1.0)})

    captured = {}
    monkeypatch.setattr(s3, '_rset', lambda key, data: captured.setdefault(key, data))
    monkeypatch.setattr(s3, '_rpublish', lambda *a, **k: None)
    monkeypatch.setattr(s3, '_log', lambda *a, **k: None)

    s3.compute_and_detect(['AAAUSDT'])
    data = captured.get('market:s3_data', {})
    w15 = data['symbols']['AAAUSDT']['15m']
    assert w15['close'] == 1.5
