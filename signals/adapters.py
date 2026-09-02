"""Signal Adapters（P2）：Legacy 输入 → Unified Signal。

纯函数、零 IO（无 Redis/Binance/DB/网络）、无副作用、不重算任何指标——
只做 读取 → 映射 → 归一（见 docs/v2/SIGNAL_MAPPING.md）。
"""
from __future__ import annotations

from typing import Any

from signals.models import Signal, SignalValidationError
from signals.normalize import normalize_side

__all__ = [
    "s6_signal",
    "s8_signal",
    "signal_from_event",
    "to_journal_builder_kwargs",
]


def signal_from_event(evt: Any, *, side: str, strategy: str) -> Signal:
    """S3 / TradingView 事件 dict → Unified Signal。

    - symbol 缺失/为空 → 严格失败（SignalValidationError）
    - source：无标记 → S3（S6/S8 的事件快照默认来自 S3）；'tv' → TRADINGVIEW
    - side：由**消费方**注入（S6→LONG / S8→SHORT）；事件自带的 side 字段
      （仅 TV 有且消费方不读）保留在 metadata
    - 原始事件整包进 metadata（含 chg_*/flow 等，供 Journal raw 与回放）
    """
    if not isinstance(evt, dict):
        raise SignalValidationError(
            f"event must be a dict, got {type(evt).__name__}")
    symbol = str(evt.get('symbol') or '').strip().upper()
    if not symbol:
        raise SignalValidationError("event missing symbol")
    side_normalized = normalize_side(side)
    return Signal(
        symbol=symbol,
        side=side_normalized,
        signal_type=evt.get('type', ''),
        source=evt.get('source') or 'S3',
        strategy=strategy,
        strength=evt.get('strength'),
        timestamp=evt.get('ts'),
        event_id=evt.get('event_id'),
        metadata=dict(evt),
    )


def s6_signal(evt: dict) -> Signal:
    """S6 消费的事件 → Unified Signal（side=LONG, strategy=S6）。"""
    return signal_from_event(evt, side='LONG', strategy='S6')


def s8_signal(evt: dict) -> Signal:
    """S8 消费的事件 → Unified Signal（side=SHORT, strategy=S8）。"""
    return signal_from_event(evt, side='SHORT', strategy='S8')


def to_journal_builder_kwargs(signal: Signal) -> dict:
    """Unified Signal → DecisionJournalBuilder kwargs（Journal 映射边界）。

    产出的字段值与 P1-02 直连 evt 的方式逐字段相同（行为保持），
    差异仅一处：TV 事件（带 ts）从此记录 signal_timestamp（此前恒 None）。
    """
    return {
        'signal_source': signal.source,
        'signal_type': signal.signal_type,
        'symbol': signal.symbol,
        'side': signal.side,
        'strength': signal.strength,
        'signal_timestamp': signal.timestamp,
        'event_id': signal.event_id,
        'raw': dict(signal.metadata),
        'strategy': signal.strategy,
    }
