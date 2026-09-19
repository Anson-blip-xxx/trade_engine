"""thread/runtime coordination glue（P8-06B；C7；无ната backing owner）。

只承载 thread/start mechanics：
- algo worker loop（pop(0)→classify→place-or-drop→sleep(11) / idle→sleep(1)），
  FIFO, no retry / no requeue
- algo start（singleton flag gate / daemon=True / name='algo-worker'）
- WS boot（PM_NO_WS guard → Thread(target=connect_loop_fn, daemon=True)）；
  `_WS_THREAD` backing 仍由 PM 持有（经 get/set callable 操作）

⚠️ **不**引入 RuntimeManager / ThreadManager / WorkerRegistry。
backing 全部由 caller 注入（late-binding；legacy pm wrapper 保留 seam）。
"""
from __future__ import annotations

import threading
import time

from position_protection.task import (
    QueueTaskClassification,
    classify_queue_task,
)


def algo_worker_loop(queue: list, lock, execute_fn, log_fn) -> None:
    """Consume immutable tasks and fail closed on legacy/malformed shapes.

    冻结：FIFO pop(0)；task place/drop → sleep(11)；idle → sleep(1)；
    无 retry；无 requeue；异常 log continue。
    """
    while True:
        task = None
        with lock:
            if queue:
                task = queue.pop(0)
        if task:
            parsed = classify_queue_task(task)
            if parsed.classification is not QueueTaskClassification.FENCED:
                log_fn(
                    '[AlgoWorkerDrop] '
                    f'symbol={parsed.symbol or "?"} '
                    f'reason={parsed.classification.value}'
                )
                time.sleep(11)
                continue
            fenced_task = parsed.task
            symbol = fenced_task.symbol
            try:
                execute_fn(fenced_task)
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
