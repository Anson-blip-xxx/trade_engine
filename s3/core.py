"""S3 Feature Core — 纯特征计算内核（P5-02 提取）。

从 strategies/s3_orderflow.py 的纯路径逐字镜像：
- ema(values, period)：全量序列计算（增量缓存路径留在 s3_orderflow，P5-02 不接）
- rsi(prices, period)：含短列表→50.0 / gains 正→100 / losses=0 边界现状
- atr(candles, period)：输入为 oldest→newest 反转后的蜡烛 dict 列表
- build_window_features(reversed_candles, ema20, ema60)：compute_window_data
  纯段逐字镜像；EMA 值由调用方（缓存层）计算后传入，键序原位

数值行为逐字冻结：int/float、除零守卫、round 位数、max 剪裁、
空/短列表 fallback。禁止 redis/requests/websocket/strategies/shared/time/IO；
无全局可变状态。事件/阈值/冷却不在此（P5-03/04 后置）。
"""
from __future__ import annotations

from typing import Optional, Sequence


def ema(values: Sequence[float], period: int) -> float:
    """EMA 全量序列计算（compute_ema 全量路径逐字镜像，含短列表 fallback）。"""
    if not values:
        return 0
    # 首次计算（全量）——与原实现逐字相同
    if len(values) < period:
        result = values[-1] if values else 0
    else:
        k = 2 / (period + 1)
        ema_v = sum(values[:period]) / period
        for v in values[period:]:
            ema_v = v * k + ema_v * (1 - k)
        result = ema_v
    return result


def rsi(prices: Sequence[float], period: int = 14) -> float:
    """RSI（compute_rsi 逐字镜像，非 textbook 标准化）。"""
    if len(prices) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(prices) - period, len(prices)):
        chg = prices[i] - prices[i-1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))


def atr(candles: Sequence[dict], period: int = 14) -> float:
    """ATR（compute_atr 逐字镜像；输入 oldest→newest）。"""
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, min(len(candles), period + 1)):
        hl = candles[i]['h'] - candles[i]['l']
        hc = abs(candles[i]['h'] - candles[i-1]['c'])
        lc = abs(candles[i]['l'] - candles[i-1]['c'])
        trs.append(max(hl, hc, lc))
    return sum(trs) / len(trs) if trs else 0


def build_window_features(reversed_candles: Sequence[dict],
                          ema20: Optional[float],
                          ema60: Optional[float]) -> dict:
    """窗口特征内核：compute_window_data 纯段逐字镜像。

    输入：oldest→newest 蜡烛 dict 列表（原 k_rev）+ 调用方算好的 ema20/60
    （保持原实现的 EMA 缓存调用序——20 先 60 后）。rounding/守护规则逐字。
    """
    k_rev = reversed_candles
    closes  = [c['c'] for c in k_rev]
    highs   = [c['h'] for c in k_rev]
    lows    = [c['l'] for c in k_rev]
    volumes = [c['v'] for c in k_rev]
    taker_buy = [c.get('tbv', 0.0) for c in k_rev]

    first_close = closes[0]
    last_close  = closes[-1]
    chg_pct = ((last_close - first_close) / first_close * 100) if first_close else 0

    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    latest_vol = volumes[-1] if volumes else 0
    total_volume = sum(volumes)
    taker_buy_volume = sum(taker_buy)
    taker_sell_volume = max(0.0, total_volume - taker_buy_volume)

    return {
        'chg':       round(chg_pct, 2),
        'atr':       round(atr(k_rev), 6),
        'atr_pct':   round(atr(k_rev) / last_close * 100, 4) if last_close else 0,
        'volume':    round(sum(volumes), 2),
        'vol_ratio': round(latest_vol / avg_vol, 2) if avg_vol > 0 else 1.0,
        'taker_buy_volume': round(taker_buy_volume, 2),
        'taker_sell_volume': round(taker_sell_volume, 2),
        'taker_buy_ratio': round(taker_buy_volume / total_volume, 4) if total_volume > 0 else 0.5,
        'orderflow_bias': round((taker_buy_volume - taker_sell_volume) / total_volume, 4)
        if total_volume > 0 else 0.0,
        'high':      round(max(highs), 8),
        'low':       round(min(lows), 8),
        'close':     round(last_close, 8),
        'rsi':       round(rsi(closes), 2),
        'ema20':     round(ema20, 6),
        'ema60':     round(ema60, 6),
        'volatility': round((max(highs) - min(lows)) / last_close * 100, 4) if last_close else 0,
        'drawdown':  round((min(lows) - max(highs)) / max(highs) * 100, 4) if max(highs) else 0,
    }
