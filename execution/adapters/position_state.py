"""Redis Position State Adapter — PositionStatePort 的 Redis helper 桥接。

职责（仅此而已）：把 Port 调用转换成**当前已有** Redis 操作：
    load_positions()  → injected redis_get('pm:positions')   （== _rget）
    save_positions(p) → injected redis_set('pm:positions', p) （== _rset）

冻结约束：
- key 逐字 'pm:positions'（KEY_MAP 映射 shared/config/pm_state.json 文件双写）
- 序列化在 helper 内部（JSON indent=2, default=str），Adapter 不做序列化
- 无 TTL；异常语义 = 镜像 PM._load_meta/_save（try/except 原样，不"改善"）
- helper（shared.redis_store）自身永不向调用方抛错（内部降级）——
  Adapter 的 try/except 是与 PM seam 一致的防御层，非新增行为
- 不创建 Redis client / 不改连接池 / 不依赖 PM / SE / PostgreSQL；
  wiring 时注入现有 helper（PM 侧为 _rget/_rset，晚绑定保留 monkeypatch 语义）
"""
from __future__ import annotations

from typing import Callable

RedisGet = Callable[..., dict]
RedisSet = Callable[..., None]

#: Position State 唯一 key（逐字冻结，禁止别名/拼接）
PM_POSITIONS_KEY = 'pm:positions'


class RedisPositionStateAdapter:
    """PositionStatePort 的 Redis 实现（注入式，含现有异常语义镜像）。

    行为逐字镜像（值过滤/异常吞错规则与 PM seam 一致）：
    - load_positions  == PM._load_meta
    - save_positions  == PM._save
    """

    def __init__(self, redis_get: RedisGet, redis_set: RedisSet) -> None:
        self._redis_get = redis_get
        self._redis_set = redis_set

    def load_positions(self) -> dict:
        """读取 pm:positions：缺 key/非 dict/值非 dict/异常 → {}（_load_meta 镜像）。"""
        try:
            local_pos = self._redis_get(PM_POSITIONS_KEY)
            if isinstance(local_pos, dict):
                return {s: p for s, p in local_pos.items() if isinstance(p, dict)}
        except Exception:
            pass
        return {}

    def save_positions(self, positions: dict) -> None:
        """整量写入 pm:positions（_save 镜像：异常静默，无 TTL，快照语义）。"""
        try:
            self._redis_set(PM_POSITIONS_KEY, positions)
        except Exception:
            pass
