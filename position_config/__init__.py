"""position_config — PositionManager 域静态常量（P8-01，Green Cleanup）。

leaf package：零依赖；import 无任何副作用（不启线程、无 IO、无 sleep）。
runtime ownership（_WS_POSITIONS / _ALGO_QUEUE / 节流 dict 等）不在此处
——一律留在 shared/position_manager.py（单 backing，identity 不变）。
"""
from position_config import constants as constants

SYSTEM_CFG = constants.SYSTEM_CFG
WS_LEASE_KEY = constants._WS_LEASE_KEY
WS_LEASE_TTL = constants._WS_LEASE_TTL
ALGO_API_COOLDOWN = constants._API_COOLDOWN
ALGO_UPDATE_INTERVAL = constants._ALGO_UPDATE_INTERVAL
ALGO_MIN_CHANGE_PCT = constants._ALGO_MIN_CHANGE_PCT
