"""PositionMonitoringService — PM 监控职责独立（P7-05B）。

import 本包无副作用：不启动线程、不访问 Redis/Binance、不 sleep。
线程 spawn 仍由 legacy 入口（shared/position_manager.py）触发。
"""
from position_monitoring.service import PositionMonitoringService

__all__ = ['PositionMonitoringService']
