"""ProtectionService（P7-04B）—— PM 保护/algo SL 职责独立层。

承载 queue enqueue、worker start、placement orchestration、cancel-id/cancel-all
编排。所有 IO via callable 注入（legacy helpers 经 late-bound factory 传入——
monkeypatch seam 保留）。**无线程、无 queue backing 独立副本、无 import 副作用**
（worker 的 Thread spawn 由 caller 注入的 thread_factory 决定）。

冻结语义（P7-04A/P4-03-01-D4）：
- FIFO `(sym, side, trigger, qty)` 4 元组（无 dict/dataclass）
- `_ALGO_WORKER_STARTED` singleton flag gate——重复 start 静默 no-op
- sleep(11) after task / sleep(1) when empty
- daemon=True name='algo-worker'
- exchangeInfo inline rounding（LOT_SIZE/PRICE_FILTER，raw requests get）
- cancel-all 在 place 之前；status ∈ (NEW/WORKING/TRIGGERED)
- PMB-9 fallback GET（不修）
- algo_sl_id 写回（经注入 callable 或 legacy save 语义）

**不负责**：open/close/partial 生命周期、monitor、reconcile。
禁止依赖 PM / Protection 状态 / Ledger / Execution 内核 —— 所有依赖经 callable
注入。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class ProtectionService:
    """Protect / Algo SL 编排服务（stateless façade——仅持有注入的 callable）。"""

    def __init__(self, *,
                 enqueue_fn: Callable[[str, str, float, float], None],
                 start_worker_fn: Callable[[], None],
                 place_algo_sl_fn: Callable[[str, str, float, float], dict],
                 cancel_id_fn: Callable[[int], dict],
                 cancel_all_fn: Callable[[str], None]):
        self._enqueue_fn = enqueue_fn
        self._start_worker_fn = start_worker_fn
        self._place_fn = place_algo_sl_fn
        self._cancel_id_fn = cancel_id_fn
        self._cancel_all_fn = cancel_all_fn

    # ── Port-compatible方法（= P4-03-01-D4 ProtectionPort 5 方法面） ─────

    def enqueue_algo_sl(self, symbol: str, side: str,
                        trigger: float, qty: float) -> None:
        """加入 algo SL 队列（FIFO 元组；不启动线程、不 place）。"""
        self._enqueue_fn(symbol, side, trigger, qty)

    def start_algo_worker(self) -> None:
        self._start_worker_fn()

    def place_algo_sl(self, symbol: str, side: str,
                      trigger_price: float, qty: float) -> dict:
        """place algo SL（exchangeInfo→cancel→post→algo_sl_id writeback）。"""
        return self._place_fn(symbol, side, trigger_price, qty)

    def cancel_algo_id(self, algo_id: int) -> dict:
        """cancel 单条件单（PMB-9 fallback GET 保留——不修）。"""
        return self._cancel_id_fn(algo_id)

    def cancel_all_algo(self, symbol: str) -> None:
        """cancel all（status ∈ NEW/WORKING/TRIGGERED 过滤冻结）。"""
        self._cancel_all_fn(symbol)
