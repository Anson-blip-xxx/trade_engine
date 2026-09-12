"""S3 State Boundary — 显式注入式进程内存状态（P5-03）。

设计（零行为变化）：
- 两个独立小边界（非 God Store）：EventStateStore（lifecycle+cooldown/dedup
  同桶）、BreakoutStateStore（FAILED_BREAKOUT 状态机）
- 均为 **dict 协议精确镜像**（duck-type dict）：get / __setitem__ / pop /
  items —— 生产函数在 state_store=None 时直接使用 legacy `_event_states`
  / `_fb_state`（普通 dict，同协议）；注入对象走本类
- dict 语义逐字：get 返回引用（aliasing）、items 迭代视图（插入序），
  pop(key, default)。**没有** defensive copy。

禁止 Redis / 文件 / 网络 / 时钟；模拟新进程 = new 实例（restart=clear 的
Golden 行为不变）。支持包一层 legacy dict 验证 staging（backing 参数）。
"""
from __future__ import annotations

from typing import Any, Optional


class EventStateStore:
    """事件生命周期状态（`SYMBOL_TYPE` → {state, strength, ts, sent_ts}）。"""

    def __init__(self, backing: Optional[dict] = None) -> None:
        self._data: dict = {} if backing is None else backing

    def get(self, key: str, default: Any = None) -> Any:
        """底层引用（不是副本——与 dict.get 逐字一致）。"""
        return self._data.get(key, default)

    def __setitem__(self, key: str, value: dict) -> None:
        self._data[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def items(self):
        """底层 dict 的 items 视图（插序 == legacy 迭代序）。"""
        return self._data.items()

    def __len__(self) -> int:
        return len(self._data)


class BreakoutStateStore:
    """FAILED_BREAKOUT 状态机（symbol → {state, last_high/low, breakout_*, ts}）。

    get 返回引用；caller 关闭时 set 落地（镜像原 `_fb_state.get(sym, default)`
    → 原地改 → `_fb_state[sym] = state` 序列）。
    """

    def __init__(self, backing: Optional[dict] = None) -> None:
        self._data: dict = {} if backing is None else backing

    def get(self, key: str, default: Any = None) -> Any:
        """返回引用；缺失时 default 原对象（非复制——caller 原地改直接进写入路径）。"""
        return self._data.get(key, default)

    def __setitem__(self, key: str, value: dict) -> None:
        self._data[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def items(self):
        return self._data.items()

    def __len__(self) -> int:
        return len(self._data)
