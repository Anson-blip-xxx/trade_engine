"""S0 Regime Classifier Core — 纯分类内核（P6-02）。

从 services/s0/s0_market_guard.compute_state 的分类主体逐字提取：
risk-off 三条件 OR（严格不等号）、5 档 regime、permit 表、score/trend_strength
分档、sentiment 叠加（地板 -7）。**无 IO / 无时钟 / 无全局可变状态**——
shared 的 wall-clock 采样结果（S0-3：alts_sync/shock_score 门控值）与
sentiment 快照由调用方（orchestration）注入，core 保持 deterministic。

除外字段（orchestration 所有）：version / timestamp。
禁止 redis/requests/clickhouse/shared/time/IO；stdlib-only。
"""
from __future__ import annotations

from typing import Any, Optional


def classify_regime(btc_trend: str, volatility: str, amp: float,
                    btc_below_ema60: bool, atr_expanding: bool,
                    breadth: str, breadth_ratio: float,
                    *,
                    alts_sync_val: float = 0.0,
                    shock_val: int = 0,
                    sentiment: Optional[dict] = None) -> dict:
    """metrics/context → market_state / regime / permits / 分档字段。

    输入 = 已采样数据；返回 = compute_state 的核心字段（dict 键序与其
    原始输出保持一致；version/timestamp 由 orchestration 前置/后置）。
    比较算式/规模裁剪与 priority 顺序逐字镜像（不允许数学等价改写）。
    """
    sent: dict = sentiment if sentiment is not None else {}

    risk_off = (
        (btc_below_ema60 and atr_expanding) or
        amp > 0.04 or
        breadth_ratio < 0.30
    )
    if risk_off:
        market_state = "risk-off"
    elif btc_trend == "bull" and breadth == "strong":
        market_state = "trend"
    else:
        market_state = "range"

    # ── 统一regime（5 档 + S6 趋势强度） ──────────────────────────────
    regime = "range"
    regime_score = 0
    trend_strength = 50

    if risk_off:
        regime = "risk-off"
        regime_score = -7
        trend_strength = 0
    elif btc_trend == "bull" and breadth == "strong":
        regime = "bull_trend"
        regime_score = 5
        trend_strength = 85
    elif btc_trend == "bull" and breadth != "weak":
        regime = "weak_bull"
        regime_score = 3
        trend_strength = 65
    elif btc_trend == "bear" or breadth_ratio < 0.35:
        regime = "weak_bear"
        regime_score = -3
        trend_strength = 25
    else:
        regime = "range"
        regime_score = 0
        trend_strength = 50

    sent = dict(sent)  # 内部 copy——不 mutate 调用方 dict（原实现消费只读）
    sentiment_risk = bool(sent.get('sentiment_risk', False))
    if sentiment_risk:
        regime_score = max(-7, regime_score - 1)  # 地板 -7（S0-3 sentiment 分支）

    # ── 各系统运行许可 ──────────────────────────────────────────────────
    s6_allowed = not risk_off or btc_trend == 'bull'
    s7_allowed = regime in ('range', 'weak_bull')   # 网格只在震荡和弱多运行
    s8_allowed = not risk_off and regime != 'bull_trend'  # 空头不能在强多头开

    return {
        "market_state":  market_state,
        "btc_trend":     btc_trend,
        "breadth":       breadth,
        "breadth_ratio": round(breadth_ratio, 3),
        "volatility":    volatility,
        "risk_off":      risk_off,
        # v1.1 新增字段
        "regime":        regime,
        "regime_score":  regime_score,
        "trend_strength": trend_strength,
        "s6_allowed":    s6_allowed,
        "s7_allowed":    s7_allowed,
        "s8_allowed":    s8_allowed,
        # v1.2 情绪叠加字段
        "fng":           sent.get('fng', 50),
        "fng_label":     sent.get('fng_label', ''),
        "avg_funding":   sent.get('avg_funding', 0.0),
        "sentiment_risk": sentiment_risk,
        "sentiment_bias": sent.get('bias', 'neutral'),
        # 采样值（wall-clock gate 的结果由 orchestration 注入，S0-3）
        "alts_sync":     alts_sync_val,
        "shock_score":   shock_val,
    }
