"""PositionLifecycleService — PM 开/平/部分平生命周期职责独立（P7-07B）。

import 本包无副作用：不启动线程、不访问 Redis/Binance、不 sleep。
生命周期线程（algo worker / WS）仍由 legacy 入口触发。
"""
from position_lifecycle.service import PositionLifecycleService

__all__ = ['PositionLifecycleService']
