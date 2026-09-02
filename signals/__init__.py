"""signal/ — Unified Signal Contract + Adapter（V2 Phase 2）。

Signal 只描述"策略发现了什么机会"：
  - 不含 Decision / Risk / Execution / Position 信息
  - source 与 strategy 分离
  - side 统一为 LONG/SHORT/NEUTRAL/UNKNOWN（BUY/SELL 属 Execution）
  - signal_type 与 PM legacy 的 event_type 语义相同（映射见 SIGNAL_MAPPING.md）

最小 Contract：symbol / side / signal_type / source / strategy / strength /
timestamp / event_id / metadata。其余原始字段保留在 metadata（事件快照）。

Adapter 为纯函数：无 IO、无副作用、不重算指标/score。
"""
from signals.adapters import (
    s6_signal,
    s8_signal,
    signal_from_event,
    to_journal_builder_kwargs,
)
from signals.enums import (
    SOURCE_AI,
    SOURCE_MANUAL,
    SOURCE_REPLAY,
    SOURCE_S3,
    SOURCE_TRADINGVIEW,
    SOURCE_UNKNOWN,
    SignalSide,
)
from signals.models import Signal, SignalValidationError, validate_signal
from signals.normalize import (
    normalize_side,
    normalize_signal_type,
    normalize_source,
    normalize_strategy,
    normalize_strength,
    normalize_symbol,
    normalize_timestamp,
)
from signals.validation import validate_signal_payload

__all__ = [
    "SOURCE_AI", "SOURCE_MANUAL", "SOURCE_REPLAY", "SOURCE_S3",
    "SOURCE_TRADINGVIEW", "SOURCE_UNKNOWN",
    "Signal",
    "SignalSide",
    "SignalValidationError",
    "normalize_side", "normalize_signal_type", "normalize_source",
    "normalize_strategy", "normalize_strength", "normalize_symbol",
    "normalize_timestamp",
    "s6_signal", "s8_signal", "signal_from_event",
    "to_journal_builder_kwargs",
    "validate_signal",
    "validate_signal_payload",
]
