"""thread/runtime coordination glue（P8-06B；C7；无ната backing owner）。

只承载 thread/start mechanics：
- algo worker loop（pop(0)→place→sleep(11) / idle→sleep(1)），
  FIFO, no retry / no requeue（行为逐字）
- algo start（singleton flag gate / daemon=True / name='algo-worker'）
- WS boot（PM_NO_WS guard → Thread(target=connect_loop_fn, daemon=True)）；
  `_WS_THREAD` backing 仍由 PM 持有（经 get/set callable 操作）

⚠️ **不**引入 RuntimeManager / ThreadManager / WorkerRegistry。
backing 全部由 caller 注入（late-binding；legacy pm wrapper 保留 seam）。
"""
from __future__ import annotations

import threading
import time


def algo_worker_loop(queue: list, lock, place_fn, log_fn) -> None:
    """后台循环（行为逐字 from PM `_algo_worker_loop`）。

    冻结：FIFO pop(0)；task → sleep(11)；idle → sleep(1)；无 retry；
    无 requeue；异常 log continue。
    """
    while True:
        task = None
        with lock:
            if queue:
                task = queue.pop(0)
        if task:
            symbol, side, trigger_price, qty = task
            try:
                place_fn(symbol, side, trigger_price, qty)
            except Exception as e:
                log_fn(f'[AlgoWorker异常] {symbol}: {e}')
            time.sleep(11)  # 限速间隔
        else:
            time.sleep(1)  # 队列空，1s 后再检查


def start_algo_worker(*, get_started, set_started, worker_loop, thread_cls,
                      log_fn) -> None:
    """启动后台队列消费线程（singleton gate；行为逐字 from PM `_algo_start_worker`）。"""
    if get_started():
        return
    set_started(True)
    t = thread_cls(target=worker_loop, daemon=True, name='algo-worker')
    t.start()
    log_fn('[AlgoWorker] 后台队列消费线程已启动')


def start_ws_thread(*, disabled: bool, connect_loop_fn, thread_cls,
                    get_thread, set_thread) -> None:
    """WS thread boot（`PM_NO_WS` guard → Thread(target=connect_loop_fn,
    daemon=True)；`_WS_THREAD` backing 由 PM 持有，经 get/set callable）。"""
    if disabled:
        return
    if get_thread() is not None:
        return                       # singleton guard（重复 start no-op）
    t = thread_cls(target=connect_loop_fn, daemon=True)
    set_thread(t)
    t.start()
