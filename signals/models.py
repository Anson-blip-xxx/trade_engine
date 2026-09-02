"""Unified Signal Contract（P2）。

Signal 只回答"策略发现了什么机会"：
  - 不含是否交易（Decision）、不含仓位大小（Risk）、不含订单动作（Execution）
  - 不含市场指标（ATR/EMA 等属 Market，随原始事件保留在 metadata）
  - source 与 strategy 分离（同一 strategy 可有多个 source，反之亦然）

全部字段来自 SIGNAL_INVENTORY.md 统计的真实数据；frozen + 归一化构造。
"""
from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field

from signals.enums import SignalSide
from signals.normalize import (
    SignalValidationError,
    normalize_side,
    normalize_signal_type,
    normalize_source,
    normalize_strategy,
    normalize_strength,
    normalize_symbol,
    normalize_timestamp,
)

__all__ = ["Signal", "SignalValidationError", "validate_signal"]


@dataclass(frozen=True)
class Signal:
    """统一 Signal 表达（最小稳定 Contract）。

    核心字段（跨策略/跨入口/跨模块需要稳定表达的信息）：
      symbol / side / signal_type / source / strategy / strength / timestamp / event_id
    其余原始字段一律进 metadata（事件快照），不做 God Signal。
    """

    symbol: str
    side: str
    signal_type: str
    source: str
    strategy: str
    strength: float | None = None
    timestamp: str | None = None
    event_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        # symbol：缺失即失败（严格）
        symbol = normalize_symbol(self.symbol)
        if not symbol:
            raise SignalValidationError("signal symbol is required")
        object.__setattr__(self, "symbol", symbol)

        # side：非法值严格失败
        try:
            object.__setattr__(self, "side", normalize_side(self.side))
        except SignalValidationError as exc:
            raise SignalValidationError(f"signal {symbol}: {exc}") from exc

        # signal_type / source / strategy：非空
        signal_type = normalize_signal_type(self.signal_type)
        if not signal_type:
            raise SignalValidationError(f"signal {symbol}: signal_type is required")
        object.__setattr__(self, "signal_type", signal_type)

        source = normalize_source(self.source)
        if not source:
            raise SignalValidationError(f"signal {symbol}: source is required")
        object.__setattr__(self, "source", source)

        strategy = normalize_strategy(self.strategy)
        if not strategy:
            raise SignalValidationError(f"signal {symbol}: strategy is required")
        object.__setattr__(self, "strategy", strategy)

        object.__setattr__(self, "strength", normalize_strength(self.strength))

        try:
            object.__setattr__(self, "timestamp", normalize_timestamp(self.timestamp))
        except SignalValidationError as exc:
            raise SignalValidationError(f"signal {symbol}: {exc}") from exc

        if not isinstance(self.metadata, dict):
            raise SignalValidationError(f"signal {symbol}: metadata must be a dict")
        # metadata 含嵌套原始事件 → 深拷贝实现真隔离（区别于 journal 的浅拷贝约定）
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))

    # signal_type 的历史别名（PM 持仓记录使用 event_type，语义相同，见 OBS-2）
    @property
    def event_type(self) -> str:
        """Legacy 别名：PM 持仓记录中的 event_type 与本字段语义相同。"""
        return self.signal_type


def validate_signal(signal: Signal) -> list[str]:
    """构造后校验（正常情况下 frozen 构造已保证；供外部防御性检查）。"""
    errors: list[str] = []
    if not signal.symbol:
        errors.append("symbol 不能为空")
    if signal.side not in tuple(s.value for s in SignalSide):
        errors.append(f"side 非法: {signal.side!r}")
    if not signal.signal_type:
        errors.append("signal_type 不能为空")
    if not signal.source:
        errors.append("source 不能为空")
    if not signal.strategy:
        errors.append("strategy 不能为空")
    try:
        dataclasses.asdict(signal)
    except (TypeError, ValueError) as exc:
        errors.append(f"不可序列化: {exc}")
    return errors
