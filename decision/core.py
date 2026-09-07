"""Pure Decision Core（Phase 3-02）。

从 shared_executor.py 原样迁移的纯 Decision 函数。
行为与 P3-01 characterization tests 冻结的版本逐字节一致。

规则：
- 只使用标准库 / 内建类型，零外部依赖
- 不访问 Redis / Binance / PM / 网络 / 文件
- 不修改全局状态、不读当前时间、不使用 random
- 不 import shared_executor / S6 / S8 / PM（禁止循环依赖）
"""
from __future__ import annotations

__all__ = [
    "CONFIRMED_SHORT_SIGNALS",
    "MIN_CONFIRMED_SHORT_STRENGTH",
    "classify_entry_mode",
    "contract_score",
    "long_signal_allows_open",
    "long_trend_takeover_ready",
    "price_is_overextended",
    "pump_down_uptrend_guard",
    "resolve_event_flow",
    "resolve_event_orderflow_bias",
    "short_signal_allows_open",
]


# ── price_is_overextended ────────────────────────────────────────────────

def price_is_overextended(price: float, ema20: float, atr: float,
                          side: str, max_atr: float) -> bool:
    """Reject entries that chase price too far from the 1h mean."""
    if price <= 0 or ema20 <= 0 or atr <= 0:
        return False
    extension = (price - ema20) / atr if side == 'LONG' else (ema20 - price) / atr
    return extension > max_atr


# ── resolve_event_flow / resolve_event_orderflow_bias ───────────────────

def resolve_event_flow(evt: dict, market_win: dict) -> float | None:
    """优先用事件自带的 taker_buy_ratio（TV 真实行情透传），否则回退 S3 市场快照。

    demo 测试网的 S3 快照里该字段退化为 0，TV webhook 补上真实值后，
    classify_entry_mode / contract_score 的 flow 确认才会真正生效。
    """
    v = evt.get('taker_buy_ratio')
    if v is not None:
        try:
            fv = float(v)
            if fv > 0:
                return fv
        except (TypeError, ValueError):
            pass
    return market_win.get('15m', {}).get('taker_buy_ratio')


def resolve_event_orderflow_bias(evt: dict, market_win: dict) -> float | None:
    v = evt.get('orderflow_bias')
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return market_win.get('15m', {}).get('orderflow_bias')


# ── classify_entry_mode ─────────────────────────────────────────────────

def classify_entry_mode(price: float, ema20: float, rsi: float,
                        taker_buy_ratio: float | None, side: str) -> str:
    """Classify a candidate as right-side momentum or confirmed reversal."""
    if side == 'LONG':
        if price < ema20 and rsi <= 35 and (taker_buy_ratio is None or taker_buy_ratio >= 0.52):
            return 'LEFT_REVERSAL'
        if price >= ema20:
            return 'RIGHT_MOMENTUM'
    else:
        if price > ema20 and rsi >= 65 and (taker_buy_ratio is None or taker_buy_ratio <= 0.48):
            return 'LEFT_REVERSAL'
        if price <= ema20:
            return 'RIGHT_MOMENTUM'
    return 'UNCONFIRMED'


# ── long_trend_takeover_ready ───────────────────────────────────────────

def long_trend_takeover_ready(price: float, market: dict) -> bool:
    """Require higher-timeframe trend plus a pullback and buyer pressure."""
    w15 = market.get('15m', {})
    w4h = market.get('4h', {})
    w24 = market.get('24h', {})
    ema15 = float(w15.get('ema20', 0) or 0)
    atr15 = float(w15.get('atr', 0) or 0)
    ema24 = float(w24.get('ema20', 0) or 0)
    ema60 = float(w24.get('ema60', 0) or 0)
    flow = w15.get('taker_buy_ratio')
    if not all((price > 0, ema15 > 0, ema24 > 0, ema60 > 0)):
        return False
    if price <= ema24 or ema24 <= ema60 or float(w4h.get('chg', 0) or 0) <= 0:
        return False
    if flow is not None and float(flow) < 0.52:
        return False
    distance = abs(price - ema15)
    return distance <= max(atr15 * 1.5, price * 0.02) if atr15 > 0 else price <= ema15 * 1.02


# ── pump_down_uptrend_guard ─────────────────────────────────────────────

def pump_down_uptrend_guard(price: float, w4h: dict, w24h: dict) -> bool:
    """Block only PUMP_DOWN shorts while higher-timeframe momentum is up."""
    ema4h = float(w4h.get('ema20', 0) or 0)
    chg4h = float(w4h.get('chg', 0) or 0)
    chg24h = float(w24h.get('chg', 0) or 0)
    return ema4h > 0 and price > ema4h and chg4h > 0 and chg24h >= 15


# ── contract_score ──────────────────────────────────────────────────────

def contract_score(strength: float, event_type: str, atr_pct: float = 0,
                   extension_atr: float = 0, taker_buy_ratio: float | None = None,
                   event_age_sec: float = 0, side: str = 'LONG',
                   short_ratio: float | None = None) -> int:
    """Combine signal quality and entry risk into a 0-100 contract score."""
    score = float(strength)
    if taker_buy_ratio is not None:
        flow_aligned = taker_buy_ratio >= 0.52 if side == 'LONG' else taker_buy_ratio <= 0.48
        score += 5 if flow_aligned else -5
    if short_ratio is not None:
        if side == 'LONG':
            score += 8 if short_ratio >= 0.60 else (-5 if short_ratio <= 0.40 else 0)
        else:
            score += 5 if short_ratio <= 0.40 else (-8 if short_ratio >= 0.60 else 0)
    if atr_pct > 4:
        score -= min(15, (atr_pct - 4) * 3)
    if extension_atr > 0:
        score -= min(15, extension_atr * 4)
    if event_age_sec > 0:
        score -= min(10, event_age_sec / 30)
    return max(0, min(100, int(round(score))))  # noqa: RUF046 — 原行为


# ── short/long signal gates ─────────────────────────────────────────────

MIN_CONFIRMED_SHORT_STRENGTH = 60
CONFIRMED_SHORT_SIGNALS = ('TREND_DOWN', 'VIOLENT_BEARISH', 'PULSE_DOWN')


def short_signal_allows_open(event_type: str, raw_strength: float) -> bool:
    if event_type not in CONFIRMED_SHORT_SIGNALS:
        return True
    return float(raw_strength or 0) >= MIN_CONFIRMED_SHORT_STRENGTH


def long_signal_allows_open(event_type: str, market_state: dict) -> bool:
    """Avoid violent long entries while S0 identifies a bearish regime."""
    if event_type != 'VIOLENT_BULLISH':
        return True
    regime = str((market_state or {}).get('regime', '')).lower()
    return regime not in ('weak_bear', 'risk-off', 'risk_off')
