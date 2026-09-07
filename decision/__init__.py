"""decision/ — Pure Decision Core（V2 Phase 3-02）。

从 shared_executor.py 原样迁移的纯 Decision 函数。
零 IO、零外部依赖、零全局状态——输入相同则输出必须相同。
"""
from decision.core import (
    CONFIRMED_SHORT_SIGNALS,
    MIN_CONFIRMED_SHORT_STRENGTH,
    classify_entry_mode,
    contract_score,
    long_signal_allows_open,
    long_trend_takeover_ready,
    price_is_overextended,
    pump_down_uptrend_guard,
    resolve_event_flow,
    resolve_event_orderflow_bias,
    short_signal_allows_open,
)

__all__ = [
    "CONFIRMED_SHORT_SIGNALS",
    "MIN_CONFIRMED_SHORT_STRENGTH",
    "classify_entry_mode",
    "contract_score",
    "long_signal_allows_open",
    "long_trend_takeover_ready",
    "price_is_overextended",
    "pump_down_uptrend_guard",
    "resolve_event_flow",
    "resolve_event_orderflow_bias",
    "short_signal_allows_open",
]
