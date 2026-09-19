"""Protection adapter — ProtectionPort 的注入式实现（零逻辑委托）。

调用注入（wiring 时绑 PM 的 real functions）：
    {_algo_enqueue, _algo_start_worker, _algo_place_sl_inner,
     _algo_cancel, _cancel_all_algo}
无 try/except / 无线程创建 / 无 sleep / 无参数加工 / 无异常包装——
语义 = 注入 callable 原样。
"""
from __future__ import annotations

from typing import Callable

EnqueueAlgoSl = Callable[..., None]
StartAlgoWorker = Callable[[], None]
PlaceAlgoSl = Callable[..., dict]
CancelAlgoId = Callable[..., dict]
CancelAllAlgo = Callable[..., None]


class ProtectionAdapter:
    """ProtectionPort 的注入式实现（惰性 wiring，零生产改动）。"""

    def __init__(self, enqueue_algo_sl: EnqueueAlgoSl,
                 start_algo_worker: StartAlgoWorker,
                 place_algo_sl: PlaceAlgoSl,
                 cancel_algo_id: CancelAlgoId,
                 cancel_all_algo: CancelAllAlgo) -> None:
        self._enqueue = enqueue_algo_sl
        self._start_worker = start_algo_worker
        self._place = place_algo_sl
        self._cancel_id = cancel_algo_id
        self._cancel_all = cancel_all_algo

    def enqueue_algo_sl(
            self, symbol: str, side: str, trigger: float, qty: float, *,
            exchange_position_key=None, episode_id=None,
            slot_generation=None, protection_generation=None) -> None:
        """Forward legacy or complete immutable identity unchanged."""
        identity = {
            'exchange_position_key': exchange_position_key,
            'episode_id': episode_id,
            'slot_generation': slot_generation,
            'protection_generation': protection_generation,
        }
        if all(value is None for value in identity.values()):
            self._enqueue(symbol, side, trigger, qty)
            return
        self._enqueue(symbol, side, trigger, qty, **identity)

    def start_algo_worker(self) -> None:
        """逐字委托（线程所有权保持 PM 实现，不新建）。"""
        self._start_worker()

    def place_algo_sl(self, symbol: str, side: str, trigger_price: float,
                      qty: float) -> dict:
        """逐字委托（returns raw dict / {'error': ...} 原样）。"""
        return self._place(symbol, side, trigger_price, qty)

    def cancel_algo_id(self, algo_id: int) -> dict:
        """逐字委托（含 PMB-9 兜底事实，契约不修）。"""
        return self._cancel_id(algo_id)

    def cancel_all_algo(self, symbol: str) -> None:
        """逐字委托（逐条 DELETE + 每条日志，原样）。"""
        self._cancel_all(symbol)
