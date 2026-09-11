"""Notification Port — 监控类通知的最小 boundary 契约。

真实 seam（行为逐字冻结，含状态机）：
- `_notify_external_position(symbol, raw, system)`：外部漏记仓告警
  （指纹 pending 30s grace + 每日去重 + TG requests.post 直连 + PG 事件）
- `_log_close_error(symbol, message, interval=60)`：平仓错误限频日志

两个 seam 均自带去重/节流状态（模块级状态 + Redis），因此契约按"含状态原样"
冻结；不含 TG 统一；不暴露 Telegram client。
当前 NO-WIRING：wiring 属 Phase 7（monitoring decomposition）。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NotificationPort(Protocol):
    """监控通知能力端口（structural typing，2 个真实 seam 逐字镜像）。"""

    def notify_external_position(self, symbol: str, raw: dict,
                                 system: str) -> None:
        """外部/漏记仓位告警（内含指纹/30s grace/每日去重/TG/PG 事件）。"""
        ...

    def log_close_error(self, symbol: str, message: str,
                        interval: int = 60) -> None:
        """平仓失败限频记录（间隔内重复静默）。"""
        ...
