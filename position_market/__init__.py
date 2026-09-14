"""position_market — PositionManager 域市场数据 leaf helpers（P8-03 启组）。

leaf package：零依赖；import 无副作用（无 HTTP/Redis/thread/sleep）。
transport（Binance IO）不迁——一律由 caller 注入 legacy callable。
"""
from position_market import funding

__all__ = ['funding']
