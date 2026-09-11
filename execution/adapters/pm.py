"""PositionManager Adapter — PositionManagerPort 的注入式实现。

与 SharedExecutorBinanceAdapter 同风格：真实实现在 wiring 时以 callable 注入
（Phase 7 前由编排层注入 shared_executor._update_pos_cache），execution 包
不 import strategies/shared（无循环依赖）。

无 try/except / 无参数加工 / 无结果包装：
异常与返回值语义 = 注入 callable 原样（E-OBS-1 的 PM 失败处理留在编排层）。
"""
from __future__ import annotations

from typing import Callable

#: 与 shared_executor._update_pos_cache 同签名（(name, symbol, side, entry,
#: qty, stop_price, leverage, margin, event_type, strength) -> position_id）
UpdatePosCache = Callable[..., str]


class PositionManagerAdapter:
    """PositionManagerPort 的 callable 注入实现（惰性 wiring，零生产改动）。"""

    def __init__(self, update_pos_cache: UpdatePosCache) -> None:
        self._update_pos_cache = update_pos_cache

    def register_opened_position(self, name: str, symbol: str, side: str,
                                 entry: float, qty: float, stop_price: float,
                                 leverage: int, margin: str, event_type: str,
                                 strength: int) -> str:
        """逐字委托：参数原样、返回值原样、异常原样。"""
        return self._update_pos_cache(
            name, symbol, side, entry, qty, stop_price,
            leverage, margin, event_type, strength)
