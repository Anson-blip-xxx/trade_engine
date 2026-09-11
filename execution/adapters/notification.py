"""Notification adapter — NotificationPort 的注入式实现（零逻辑委托）。"""
from __future__ import annotations

from typing import Callable

NotifyExternalPosition = Callable[..., None]
LogCloseError = Callable[..., None]


class NotificationAdapter:
    """NotificationPort 的注入式实现（惰性 wiring，零生产改动）。

    seam 各自带去重/节流状态——语义 = 注入 callable 原样（含状态机）。
    """

    def __init__(self, notify_external_position: NotifyExternalPosition,
                 log_close_error: LogCloseError) -> None:
        self._notify_ext = notify_external_position
        self._log_close_err = log_close_error

    def notify_external_position(self, symbol: str, raw: dict,
                                 system: str) -> None:
        """逐字委托（指纹/grace 去重状态原样）。"""
        self._notify_ext(symbol, raw, system)

    def log_close_error(self, symbol: str, message: str,
                        interval: int = 60) -> None:
        """逐字委托（默认间隔 60 与 PM seam 一致）。"""
        self._log_close_err(symbol, message, interval)
