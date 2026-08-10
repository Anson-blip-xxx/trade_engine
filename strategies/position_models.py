"""Reusable Binance position-sizing models."""
from dataclasses import dataclass


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
