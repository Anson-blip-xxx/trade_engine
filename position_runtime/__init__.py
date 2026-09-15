"""position_runtime — PositionManager thread/runtime coordination glue（P8-06B）。

仅承载 thread/start mechanics——**不 owning 任何 runtime backing**：
  `_ALGO_QUEUE` / `_ALGO_QUEUE_LOCK` / `_ALGO_WORKER_STARTED` /
  `_WS_POSITIONS` / `_WS_LOCK` / `_WS_LAST_UPDATE` / `_WS_THREAD` /
  `_monitor_heartbeat_ts` / heartbeat / recently-ghosted —— 依旧 PM 单 backing；
  runtime 函数经注入 callable/mutable reference 操作（无第二份）。
import 本包副作用：0（无 thread/Redis/HTTP/sleep/queue/flag mutation）。
"""
from position_runtime.runtime import (
    start_algo_worker, algo_worker_loop, start_ws_thread,
)

__all__ = ['start_algo_worker', 'algo_worker_loop', 'start_ws_thread']
