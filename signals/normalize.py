"""Signal 归一化规则（P2）。

全部为确定性纯函数：不依赖 locale / 当前时间 / 网络 / 随机。
未知 side 直接抛 SignalValidationError（严格失败，见 P2 §20）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from signals.enums import SignalSide

__all__ = [
    "SignalValidationError",
    "normalize_side", "normalize_signal_type", "normalize_source",
    "normalize_strategy", "normalize_strength", "normalize_symbol",
    "normalize_timestamp",
]


class SignalValidationError(ValueError):
    """Signal 构造/归一失败（严格失败，上层决定如何处理）。"""


def normalize_symbol(value: Any) -> str:
    """strip + upper（项目 symbol 约定：BTCUSDT）。"""
    return str(value or "").strip().upper()


def normalize_side(value: Any) -> str:
    """side 归一：BUY→LONG、SELL→SHORT、大小写/空格容忍、未知 → 严格失败。"""
    if value is None:
        return SignalSide.UNKNOWN.value
    v = str(value).strip().upper()
    if v in ("", "NONE"):
        return SignalSide.UNKNOWN.value
    if v == "BUY":
        return SignalSide.LONG.value
    if v == "SELL":
        return SignalSide.SHORT.value
    if v in tuple(s.value for s in SignalSide):
        return v
    raise SignalValidationError(f"unknown side: {value!r}")


def normalize_signal_type(value: Any) -> str:
    """signal_type 归一：strip + upper（PULSE_UP / TREND_DOWN / ...）。"""
    return str(value or "").strip().upper()


def normalize_source(value: Any) -> str:
    """source 归一：strip + upper；'tv' → 'TRADINGVIEW'。开放集合，不硬限制。"""
    v = str(value or "").strip().upper()
    if v in ("", "NONE"):
        return "UNKNOWN"
    if v == "TV":
        return "TRADINGVIEW"
    return v


def normalize_strategy(value: Any) -> str:
    """strategy 归一：strip + upper（S6 / S8 / ...）。"""
    return str(value or "").strip().upper()


def normalize_strength(value: Any) -> float | None:
    """strength → float；None/解析失败 → None（记录原始语义，不猜）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_timestamp(value: Any) -> str | None:
    """timestamp 归一：epoch 秒/datetime → UTC ISO-8601；合法 ISO 字符串原样；
    None → None；无法解析 → 严格失败。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise SignalValidationError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SignalValidationError(f"invalid timestamp: {value!r}") from exc
        return v
    raise SignalValidationError(f"invalid timestamp: {value!r}")
