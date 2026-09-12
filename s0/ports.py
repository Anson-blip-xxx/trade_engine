"""S0 Publisher Port — 分类结果三写链的最小契约（P6-03）。

真实 seam：`services/s0/s0_market_guard.write_state`（Redis → 原子文件 →
ClickHouse，S0-9 三种不同失败语义）。契约保留**单一方法**——当前 production
contract 是一条有顺序、有异常语义的链，不拆成三个方法。

不得 import：Redis client / clickhouse / services.s0 / shared / strategies /
execution / classifier core（s0.core）；无时钟/无网络/无文件 IO。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class S0PublisherPort(Protocol):
    """S0 状态发布能力端口（structural typing；adapter 无需显式继承）。"""

    def publish_state(self, state: dict) -> None:
        """按真实三写链发布一帧市场状态。

        语义（S0-9 逐字镜像，由 adapter 实现）：
        - Redis 失败 → 吞错继续（file/CH 照写）
        - 原子文件失败 → 异常上抛（CH 跳过）
        - CH 失败 → 不上抛（调用方视为成功，error 由 on_ch_error 回调处理）
        - 不 mutate state；不返回值
        """
        ...
