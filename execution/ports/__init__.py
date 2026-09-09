"""Execution ports — Service 的依赖边界（能力协议，无 IO 实现）。"""
from execution.ports.binance import BinanceExecutionPort

__all__ = ['BinanceExecutionPort']
