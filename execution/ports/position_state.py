"""Position State Port — PM 持仓状态（pm:positions）的最小 Redis 边界契约。

范围决策（P4-03-01-D2 / REDIS_BOUNDARY_INVENTORY.md）：
- 本 boundary **只覆盖 Position State**（key `pm:positions`，A 类）
- Closed Marker（`closed:{sym}`）/ 锁（`pm:monitor:writer` 等）/ 告警 /
  Risk / 冷却 / 事件类 key **不在**本 Port（B-G 类，现状保留）
- contract 从真实代码导出：`PM._load_meta` / `PM._save` 是 pm:positions
  仅有的两个成对读写 seam（se._update_pos_cache 的 read-modify-write 属
  open 路径独立模式，见 PMB-1，不在本 Port）
- 真值模型：pm:positions 是本地元数据层（三层加载 WS→REST→meta 的第三层、
  交易所 positionRisk 为真相来源、PG 为台账），**不是唯一真相**——
  命名与文档按 "position state store" 陈述，不称 source of truth
- Redis key/value 格式/TTL(无)/异常语义全部由 helper 与本 Port 契约冻结
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PositionStatePort(Protocol):
    """PM 持仓状态存储的最小能力端口（structural typing）。

    契约逐字镜像现有 seam：
    - load_positions == PM._load_meta（key/pm:positions、dict 过滤、异常→{}）
    - save_positions == PM._save（整量快照写入，异常静默，无 TTL）
    """

    def load_positions(self) -> dict:
        """读取 pm:positions 快照。

        - key 缺失 / 顶层非 dict / 底层非 dict 值 / Redis 异常 → {}
        - 语义与值过滤规则与 PM._load_meta 逐字一致
        """
        ...

    def save_positions(self, positions: dict) -> None:
        """整量写入 pm:positions 快照。

        - 异常静默（与 PM._save 一致）；无 TTL；不做 per-symbol 增删接口
        - 底层 helper 的文件双写（double_write=True 默认）原样保持
        """
        ...
