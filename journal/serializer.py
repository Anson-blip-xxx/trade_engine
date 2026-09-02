"""Decision Journal 序列化（P1-01）。

约定：
- datetime  → UTC ISO-8601 字符串（毫秒精度，Z 后缀）
- Enum      → 其字符串值
- Decimal   → 字符串（保留精度；本阶段项目价格计算未用 Decimal，
              若未来引入，序列化层不会悄悄 float 化）
- tuple/set → list
- 未知对象  → str() 兜底，保证 to_json 永远产出合法 JSON
- to_json 使用 sort_keys=True + allow_nan=False，同一 Journal 输出字节级稳定
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

from journal.models import (
    DecisionJournal,
    DecisionResult,
    GateResult,
    JournalValidationError,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    RiskSnapshot,
    SignalSnapshot,
    StrategySnapshot,
    validate_journal,
)

__all__ = ["from_dict", "from_json", "to_dict", "to_json"]


def _json_safe(obj: Any) -> Any:
    """递归转换为 JSON 安全结构。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Decimal):
        return str(obj)  # 精度优先：Decimal 不 float 化
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        from journal.models import iso_utc
        return iso_utc(obj)
    if isinstance(obj, MappingProxyType):
        obj = dict(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _json_safe(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    return str(obj)  # 兜底：保证永远可序列化


def to_dict(journal: DecisionJournal) -> dict:
    """DecisionJournal → 纯 JSON 结构 dict。"""
    return _json_safe(journal)


def to_json(journal: DecisionJournal, *, sort_keys: bool = True) -> str:
    """DecisionJournal → JSON 字符串（确定性输出）。"""
    return json.dumps(to_dict(journal), ensure_ascii=False, sort_keys=sort_keys, allow_nan=False)


def _build(cls, data: dict | None):
    """从 dict 构建冻结 dataclass：忽略未知键，容忍 None。"""
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in (data or {}).items() if k in known}
    return cls(**kwargs)


def from_dict(data: dict) -> DecisionJournal:
    """dict → DecisionJournal（容忍 datetime/Enum 值），并做结构校验。"""
    d = dict(data or {})
    journal = DecisionJournal(
        journal_version=d.get("journal_version") or "",
        journal_id=d.get("journal_id") or "",
        created_at=d.get("created_at") or "",
        decision_at=d.get("decision_at") or "",
        signal=_build(SignalSnapshot, d.get("signal") or {}),
        market=_build(MarketSnapshot, d.get("market") or {}),
        regime=_build(RegimeSnapshot, d.get("regime") or {}),
        strategy=_build(StrategySnapshot, d.get("strategy") or {}),
        gates=tuple(_build(GateResult, g) for g in (d.get("gates") or [])),
        decision=_build(DecisionResult, d.get("decision") or {}),
        risk=_build(RiskSnapshot, d["risk"]) if d.get("risk") is not None else None,
        metadata=_build(Metadata, d.get("metadata") or {}),
    )
    errors = validate_journal(journal)
    if errors:
        raise JournalValidationError("; ".join(errors))
    return journal


def from_json(text: str) -> DecisionJournal:
    """JSON 字符串 → DecisionJournal。"""
    return from_dict(json.loads(text))
