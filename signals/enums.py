"""Signal 枚举与常量（P2）。

source 是**开放集合**（与 P1-03 Journal 的 source 设计一致）：
这里只定义当前已知的文档化取值，不做 enum 硬限制。
"""
from enum import Enum

__all__ = [
    "SOURCE_AI", "SOURCE_MANUAL", "SOURCE_REPLAY", "SOURCE_S3",
    "SOURCE_TRADINGVIEW", "SOURCE_UNKNOWN", "SignalSide",
]


class SignalSide(str, Enum):
    """交易方向（Signal 语义）。BUY/SELL 属于 Execution，不在此列。"""

    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


# 已知 source 文档化取值（开放集合：新来源直接传字符串即可）
SOURCE_S3 = "S3"
SOURCE_TRADINGVIEW = "TRADINGVIEW"
SOURCE_AI = "AI"
SOURCE_MANUAL = "MANUAL"
SOURCE_REPLAY = "REPLAY"
SOURCE_UNKNOWN = "UNKNOWN"
