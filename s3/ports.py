"""S3 Publisher Port — Redis 输出边界（P5-05）。

真实语义冻结（strategies/s3_orderflow.compute_and_detect 输出段逐字镜像）：
- `event:s3` = **latest-slot 全量覆盖**（空事件也写 `{ts, events: []}`，S3-1）
- 非空事件 → `sort(key=-strength)`（S3-9：写层排序，detector 输出序≠最终序）→
  **单 try 内** set + publish（set 失败则 publish 不执行——真实吞错拓扑）
- `market:s3_data` = {'ts', 'symbols'} 全量覆盖（总是写，独立 try）
- publish channel/message='1' 恒为 's3:event:notify'/'1'
- 吞错语义（try/except pass）在 adapter 内镜像——不做统一 raise/bool

rolling cache / signal:s3_signals / mover:s3_spot 的写入位于线程闭包中
（KlineManager.save_cache 与 ws_big_order_loop），本阶段 contract 保持
冻结（测试覆盖），**不走本 port**（S3_PHASE5_CLOSURE.md §7）。
"""
from __future__ import annotations

from typing import Callable, Optional

RedisSet = Callable[..., None]
RedisPublish = Callable[..., bool]

#: key/channel 逐字冻结
EVENT_KEY = 'event:s3'
MARKET_KEY = 'market:s3_data'
NOTIFY_CHANNEL = 's3:event:notify'


class S3RedisPublisher:
    """Redis 输出适配器（注入式 redis_set / redis_publish）。

    行为 = compute_and_detect 输出段逐字镜像（含 sort 位置与单 try 吞错）。
    不创建 Redis client、不改序列化/TTL/重试。
    """

    def __init__(self, redis_set: RedisSet, redis_publish: RedisPublish) -> None:
        self._rset = redis_set
        self._rpublish = redis_publish

    def write_event_snapshot(self, events: list, ts: float) -> None:
        """全量覆盖 event:s3；events 为空 → 仍写空快照且不 publish；
        非空 → 原地 sort(-strength)（S3-9 冻结：写层排序）→ set + publish
        在**同一个 try** 内（set 失败 → publish 跳过）。"""
        if events:
            events.sort(key=lambda x: -x['strength'])
            evt_data = {'ts': ts, 'events': events}
            try:
                self._rset(EVENT_KEY, evt_data)
                self._rpublish(NOTIFY_CHANNEL)
            except Exception:
                pass
        else:
            try:
                self._rset(EVENT_KEY, {'ts': ts, 'events': []})
            except Exception:
                pass

    def write_market_snapshot(self, symbols: dict, ts: float) -> None:
        """全量覆盖 market:s3_data（独立 try，吞错）。"""
        try:
            self._rset(MARKET_KEY, {'ts': ts, 'symbols': symbols})
        except Exception:
            pass
