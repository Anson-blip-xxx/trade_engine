"""Binance Execution Port — ExecutionService 对 Binance 的最小能力协议。

设计约束（EXECUTION_SERVICE_INVENTORY.md §4-§5 / P4-03-01-A）：
- 只声明本阶段需要的能力：提交一张订单意图（execute_order 的最小依赖）。
- 不暴露 requests / SDK / HTTP path 细节（路径与参数映射属 Adapter 层）。
- 返回值 = 交易所原始响应（dict）或 None —— **不做错误归一化/包装**
  （None 与异常语义由所选 Adapter 的底层实现决定，见 adapters/binance.py）。
- 不包含 sandbox 判断（E-OBS-11a：拦截留在现有实现内，随注入实现生效）。
- 不含 retry / backoff / fallback（当前不存在，禁止新增）。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from execution.core import OrderIntent


@runtime_checkable
class BinanceExecutionPort(Protocol):
    """下单能力端口（structural typing，Adapter 无需显式继承）。"""

    def place_order(self, intent: OrderIntent) -> Optional[dict]:
        """提交一张订单意图。

        返回：交易所原始响应 dict；或 None（底层异常语义，由 Adapter 决定）。
        实现：不得修改 intent、不得新增参数、不得包装/解析响应。
        """
        ...
