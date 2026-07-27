"""Technical indicators for S0 Market Guard + S6 Auto Trader"""
from typing import List
import math


def calc_ema(closes: List[float], period: int) -> float:
    """EMA 计算"""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    multiplier = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_ema_slope(closes: List[float], period: int = 20) -> float:
    """EMA 斜率（用最近的 5 个值计算线性回归斜率）"""
    if len(closes) < period + 5:
        return 0.0
    ema = calc_ema(closes, period)
    # 用 calc_ema 的结果只是单一值，需要复用 calc_ema 计算过程
    # 重新计算 EMA 序列
    if len(closes) < period:
        return 0.0
    multiplier = 2.0 / (period + 1)
    ema_val = sum(closes[:period]) / period
    ema_vals = [ema_val]
    for price in closes[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val
        ema_vals.append(ema_val)
    # 取最后 5 个 EMA 值
    recent = ema_vals[-5:]
    if len(recent) < 2:
        return 0.0
    # 简单线性回归
    n = len(recent)
    x_avg = (n - 1) / 2.0
    y_avg = sum(recent) / n
    num = sum((i - x_avg) * (y - y_avg) for i, y in enumerate(recent))
    den = sum((i - x_avg) ** 2 for i in range(n))
    return num / den if den else 0.0


def calc_atr(candles: List, period: int = 14) -> float:
    """ATR 计算
    candles: [{h,l,pc}] 或 [{high,low,close}] 或 Binance K-line list [[t,o,h,l,c,v,...]]
    """
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i-1]
        # Handle both dict and list formats
        if isinstance(c, dict):
            high = float(c.get('h', c.get('high', 0)))
            low = float(c.get('l', c.get('low', 0)))
            prev_close = float(pc.get('c', pc.get('close', 0)))
        else:
            high = float(c[2]) if len(c) > 2 else 0   # Binance: index 2 = high
            low = float(c[3]) if len(c) > 3 else 0     # index 3 = low
            prev_close = float(pc[4]) if len(pc) > 4 else 0  # index 4 = close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)


def calc_avg_atr(candles: List, periods: List[int] = None) -> float:
    """多周期 ATR 平均值"""
    if periods is None:
        periods = [7, 14, 21]
    vals = [calc_atr(candles, p) for p in periods]
    return sum(vals) / len(vals) if vals else 0.0


def calc_rsi(closes: List[float], period: int = 14) -> float:
    """RSI 计算"""
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_macd_hist(closes: List[float]) -> float:
    """MACD 柱状图值 fast=12, slow=26, signal=9"""
    if len(closes) < 26:
        return 0.0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd = ema12 - ema26
    # 计算 signal (EMA of MACD) - 需要 MACD 序列
    # 近似：用当前值
    return macd  # 简化版


def calc_vol_ratio(volumes: List[float], period: int = 14) -> float:
    """成交量比率"""
    if len(volumes) < period:
        return 1.0
    recent = sum(volumes[-5:]) / 5.0 if len(volumes) >= 5 else sum(volumes) / len(volumes)
    avg = sum(volumes[-period:]) / period
    return recent / avg if avg > 0 else 1.0
