"""PositionReconcileService — PM 幽灵/对账职责独立（P7-06B）。

import 本包无副作用：不启动线程、不访问 Redis/Binance、不 sleep。
线程/全局 runtime 均不受 import 影响（legacy 入口仍为触发点）。
"""
from position_reconcile.service import PositionReconcileService

__all__ = ['PositionReconcileService']
