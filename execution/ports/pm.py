"""PositionManager Port — execution result → PM state 的最小契约。

边界决策（P4-03-01-D1 / PM_BOUNDARY_INVENTORY.md）：
- 当前真实代码中，open 路径的"PM 更新"是唯一跨模块的 execution-result seam：
  `shared_executor._update_pos_cache`（写 _POS_CACHE + 直写 pm:positions）
- close/partial 的结果处理完全在 PM 内部（无跨模块缝），本阶段不为它发明接口
- 本 Port **不被 ExecutionService 消费**（Service 只负责 intent→port→result，
  PM 更新属于编排层——E-OBS-1/E-OBS-2 顺序冻结）；未来 Phase 7 由编排层注入
- 不暴露 Redis / PostgreSQL / Telegram / Binance / 策略判断
- 依赖方向：orchestration → execution.ports（协议）→ PM 实现（callable 注入），
  execution 包不 import strategies/shared（无循环依赖）
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PositionManagerPort(Protocol):
    """PM 面向 execution 结果的最小能力端口（structural typing）。

    契约 = `shared_executor._update_pos_cache` 现有签名逐字镜像
    （参数名/顺序/返回值，见 tests/execution/test_pm_boundary.py parity）。
    """

    def register_opened_position(self, name: str, symbol: str, side: str,
                                 entry: float, qty: float, stop_price: float,
                                 leverage: int, margin: str, event_type: str,
                                 strength: int) -> str:
        """登记一笔已成交的开仓，返回 position_id。

        实现职责（Phase 7 前 = _update_pos_cache 现状）：
        写入 position state（含 pm:positions 持久化语义与静默吞错行为）。
        实现：不得吞异常语义变化、不得修改 pm:positions 双写方结构。
        """
        ...
