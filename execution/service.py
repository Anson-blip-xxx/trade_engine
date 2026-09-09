"""Execution Service — IO / orchestration 边界（P4-03-01-A：最小骨架）。

本阶段职责（仅此而已）：
    OrderIntent → BinanceExecutionPort.place_order → 原始响应 + 拒绝标记

不负责（后续阶段，见 EXECUTION_SERVICE_INVENTORY.md §5.4 分步表）：
- open / close / partial close 的编排迁移（P4-03-01-B/C）——shared_executor
  生产调用链原样，本阶段不接入（service.py 是"旁边建起来"）
- PM 注册 / Redis / PG / TG / Algo（P4-03-01-D）
- sandbox（拦截留在现有实现内，随注入的 Adapter 生效，E-OBS-11a）
- retry / fallback / 错误归一化 / 异常包装（当前不存在，禁止新增）

纯逻辑（side mapping / rounding / fill classification / PnL）一律复用
execution/core.py，本模块不重复实现。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from execution.core import OrderIntent, is_rejected
from execution.ports.binance import BinanceExecutionPort


@dataclass(frozen=True)
class OrderExecution:
    """execute_order 的结果。

    - intent：原样回传（同一对象，不做拷贝/转换）
    - raw：交易所原始响应原样（None = 底层异常语义，随 Adapter）
    - rejected：core.is_rejected 的**原始 truthy 值**（None / code 原值 / True），
      不转 bool —— 与 se.open_position 的判断语义逐字一致（N7，P4-03-00）。
      注意：{} 视为拒绝（True），code=0 为 falsy（不拒绝）。
    成交解析（avgPrice fallback 等，需 entry_price 业务参数）留给
    orchestration 阶段调用 core.parse_execution_result，本层不做。
    """
    intent: OrderIntent
    raw: Optional[dict]
    rejected: Any


class ExecutionService:
    """最小 Execution Service：intent → Port → result 的传递边界。

    无状态（除注入的 port）：连续调用互不影响。
    不直接 import requests / binance / shared_executor —— 全部经 port 注入。
    """

    def __init__(self, binance: BinanceExecutionPort) -> None:
        self._binance = binance

    def execute_order(self, intent: OrderIntent) -> OrderExecution:
        """提交一张订单意图并返回结果。

        - 不修改 intent（对象原样传给 port）
        - 不解析/包装 port 返回值（raw 原样）
        - port 抛出的异常原样上抛（无 retry/fallback）
        """
        raw = self._binance.place_order(intent)
        return OrderExecution(intent=intent, raw=raw, rejected=is_rejected(raw))
