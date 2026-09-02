"""Replay 数据模型（P1-03）：Difference / ComparisonResult / ReplayResult。

全部 frozen + 无时间戳/无随机值：同一输入的 ReplayResult 必须字节级一致（确定性）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["ComparisonResult", "Difference", "ReplayResult"]


@dataclass(frozen=True)
class Difference:
    """一处决策差异。path 可精确定位，如 'decision.action' / 'gates[3].passed'。"""

    path: str
    expected: Any
    actual: Any
    reason: str = ""


@dataclass(frozen=True)
class ComparisonResult:
    """Comparator 的输出：same + 全部差异 + 第一处差异路径。"""

    same: bool
    differences: tuple[Difference, ...] = ()
    first_difference: str | None = None


@dataclass(frozen=True)
class ReplayResult:
    """Replay Runner 的输出。

    语义（失败与不一致是两个独立状态，不可混同）：
      replayed=False                → Replay 未成功完成（输入非法等），见 metadata['error']
      replayed=True  + same=True    → Replay 成功，决策一致
      replayed=True  + same=False   → Replay 成功，但决策存在差异（见 first_difference）
    """

    journal_id: str | None = None
    replayed: bool = False
    same: bool = False
    original_action: str | None = None
    replay_action: str | None = None
    original_accepted: bool | None = None
    replay_accepted: bool | None = None
    original_reason: str | None = None
    replay_reason: str | None = None
    original_final_score: float | None = None
    replay_final_score: float | None = None
    first_difference: str | None = None
    differences: tuple[Difference, ...] = ()
    metadata: dict = field(default_factory=dict)
