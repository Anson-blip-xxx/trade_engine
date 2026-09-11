"""Protection Port — Algo SL / 保护性条件单的最小 boundary 契约。

真实 seam（shared/position_manager.py，行为逐字冻结）：
- `_algo_enqueue(symbol, side, trigger_price, qty)`：进程内 FIFO 入队
- `_algo_start_worker()`：daemon 线程启动（进程级单例；import 副作用现址保留）
- `_algo_place_sl_inner(symbol, side, trigger, qty) -> dict`：exchangeInfo 舍入
  → cancel-all → POST algoOrder（异常→{'error':...}）
- `_algo_cancel(algo_id) -> dict`：按 id 取消（见 PMB-9 兜底 GET 事实——契约不修）
- `_cancel_all_algo(symbol)`：查 allAlgoOrders（NEW/WORKING/TRIGGERED）→ 逐条 DELETE

本 Port 不含线程所有权变化/不建线程/不 sleep（这些属 PM 实现，保持现址）。
当前 NO-WIRING：调用方（se/PM 编排）仍直调 PM 函数；wiring 属 Phase 7。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtectionPort(Protocol):
    """Algo 保护单能力端口（structural typing，5 个真实 seam 逐字镜像）。"""

    def enqueue_algo_sl(self, symbol: str, side: str, trigger: float,
                        qty: float) -> None:
        """入队一个 Algo SL 任务（FIFO；不执行、不启动线程）。"""
        ...

    def start_algo_worker(self) -> None:
        """启动队列消费 daemon 线程（幂等；与 PM 现有单例语义一致）。"""
        ...

    def place_algo_sl(self, symbol: str, side: str, trigger_price: float,
                      qty: float) -> dict:
        """实际提交 Algo 条件止损单（由调用方保证限速）。"""
        ...

    def cancel_algo_id(self, algo_id: int) -> dict:
        """按 id 取消一个条件单。"""
        ...

    def cancel_all_algo(self, symbol: str) -> None:
        """取消该 symbol 全部活跃条件单（NEW/WORKING/TRIGGERED）。"""
        ...
