"""Pure Risk Core（Phase 3-04A）。

从 shared_executor.py / position_models.py 原样迁移的纯 Risk 函数。
行为与 P3-03 characterization tests 冻结的版本逐字节一致。

规则：
- 只使用标准库 / dataclass，零外部依赖
- 不访问 Redis / Binance / PM / 网络 / 文件
- 不修改全局状态、不读当前时间、不使用 random
- 不 import shared_executor / S6 / S8 / PM
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AtrRiskPositionSizer",
    "bounded_stop_pct",
    "leverage_for_score",
    "score_to_fraction",
]


# ── AtrRiskPositionSizer（原 position_models.py 原样迁移） ────────────────

@dataclass(frozen=True)
class AtrRiskPositionSizer:
    pool_budget: float = 0.80
    min_allocation: float = 0.03
    max_allocation: float = 0.15
    risk_per_trade: float = 0.01
    min_notional: float = 10.0

    def score_fraction(self, score: float) -> float:
        return min(self.max_allocation,
                   max(self.min_allocation, float(score) / 100 * self.max_allocation))

    def budget(self, balance: float, remaining: float, score: float,
               leverage: int, atr_pct: float = 0, stop_pct: float = 0) -> float:
        position_usdt = max(0.0, min(balance * self.pool_budget, remaining))
        position_usdt *= self.score_fraction(score)
        if atr_pct > 4:
            position_usdt *= max(0.2, 4.0 / atr_pct)
        if stop_pct > 0:
            position_usdt = min(position_usdt,
                                balance * self.risk_per_trade / (leverage * stop_pct))
        return max(position_usdt, self.min_notional)


# ── score_to_fraction ────────────────────────────────────────────────────

def score_to_fraction(score: float) -> float:
    """信号评分 → 资金池分配比例（3%~15%）。"""
    return AtrRiskPositionSizer(
        min_allocation=0.03,
        max_allocation=0.15,
        min_notional=10.0,
    ).score_fraction(score)


# ── leverage_for_score ──────────────────────────────────────────────────

def leverage_for_score(event_type: str, score: int, atr_pct: float = 0) -> int:
    """Choose leverage conservatively from quality and volatility."""
    base = {
        'PULSE_UP': 5, 'PULSE_DOWN': 5, 'PANIC_SELL': 5,
        'TREND_UP': 3, 'TREND_DOWN': 3,
        'VIOLENT_BULLISH': 3, 'VIOLENT_BEARISH': 3,
        'PUMP_UP': 2, 'PUMP_DOWN': 2,
    }.get(event_type, 3)
    if score < 60:
        return min(base, 2)
    if score < 85 or atr_pct >= 4:
        return min(base, 3)
    return base


# ── bounded_stop_pct ────────────────────────────────────────────────────

def bounded_stop_pct(base_stop_pct: float, atr_pct: float,
                     max_stop_pct: float = 0.08) -> float:
    """Apply ATR expansion without allowing an unbounded stop distance."""
    stop_pct = max(float(base_stop_pct), float(atr_pct) * 2 / 100) if atr_pct > 0 else float(base_stop_pct)
    return min(stop_pct, max_stop_pct)
