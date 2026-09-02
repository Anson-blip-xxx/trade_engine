"""Journal Recorder — DecisionJournal 的消费端（P1-02）。

P1-02 只提供最小实现：
- NullJournalRecorder   默认 no-op（生产默认，零 IO）
- MemoryJournalRecorder 内存有界暂存（测试 / 调试用）
- safe_record           fail-open 组装+记录（任何异常不影响交易链路）

不要在此接入 Redis / DB / 文件 / 线程 —— 属于 P1-03+ 的 Recorder Adapter。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

__all__ = [
    "JournalRecorder",
    "MemoryJournalRecorder",
    "NullJournalRecorder",
    "get_default_recorder",
    "safe_record",
    "set_default_recorder",
]


class JournalRecorder:
    """Recorder 接口：S6/S8 只依赖本接口，不依赖具体实现。"""

    def record(self, journal) -> None:
        """记录一个 DecisionJournal。默认 no-op。"""
        return


class NullJournalRecorder(JournalRecorder):
    """生产默认：什么都不做。Recorder 崩溃也不存在（no-op）。"""

    def record(self, journal) -> None:
        return None


class MemoryJournalRecorder(JournalRecorder):
    """内存有界暂存（测试 / 调试）。非持久化。"""

    def __init__(self, maxlen: int = 1000):
        self._items: deque = deque(maxlen=max(1, int(maxlen)))

    def record(self, journal) -> None:
        self._items.append(journal)

    @property
    def records(self) -> list:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


_default_recorder: JournalRecorder = NullJournalRecorder()


def get_default_recorder() -> JournalRecorder:
    return _default_recorder


def set_default_recorder(recorder: JournalRecorder) -> None:
    """替换进程级默认 Recorder（测试 / 未来 Adapter 接入点）。"""
    global _default_recorder
    _default_recorder = recorder if recorder is not None else NullJournalRecorder()


def safe_record(recorder: JournalRecorder, builder, *, action: str,
                accepted: bool, reason: str | None = None,
                final_score: Any = None, decision_at: Any = None,
                on_error: Callable[[str], None] | None = None) -> None:
    """组装 Journal 并交给 Recorder；**任何异常 fail-open**。

    本函数保证不向调用方（S6/S8 交易链路）抛出任何异常：
    - builder.build() 失败（返回 None）→ 跳过记录
    - recorder.record() 抛异常         → 吞掉，经 on_error 上报一条摘要日志
    - on_error 自身再抛异常            → 二次吞掉
    日志内容不含 signal raw / 市场数据 / 密钥（只含异常类型与消息摘要）。
    """
    try:
        journal = builder.build(action=action, accepted=accepted, reason=reason,
                                final_score=final_score, decision_at=decision_at)
        if journal is not None:
            recorder.record(journal)
    except Exception as exc:  # noqa: BLE001 — fail-open 是设计要求
        if on_error is not None:
            try:
                on_error(f'Decision Journal 记录失败(fail-open): '
                         f'{type(exc).__name__}: {exc}')
            except Exception:  # noqa: BLE001, S110 — 日志失败不能盖过交易异常
                pass
