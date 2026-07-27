"""Stub — 指标函数（保持 s6_auto_trader import 通畅）"""
from typing import List


def calc_ema(closes: List[float], period: int = 20) -> float:
    return closes[-1] if closes else 0.0

def calc_ema_slope(closes: List[float], period: int = 20) -> float:
    return 0.0

def calc_atr(candles: List, period: int = 14) -> float:
    return 0.0

def calc_avg_atr(candles: List, periods: List[int] = None) -> float:
    return 0.0

def calc_rsi(closes: List[float], period: int = 14) -> float:
    return 50.0

def calc_macd_hist(closes: List[float]) -> float:
    return 0.0

def calc_vol_ratio(volumes: List[float], period: int = 14) -> float:
    return 1.0
