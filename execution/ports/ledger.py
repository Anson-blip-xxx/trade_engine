"""Position Ledger Port — PG 交易台账的最小 boundary 契约。

范围决策（P4-03-01-D3 / LEDGER_BOUNDARY_INVENTORY.md）：
- 真实代码中有两个台账 seam（shared/postgres_client.py）：
  - `record_trade_event`：trade_events（事件级：OPEN/CLOSE/PARTIAL/FLAT 等）
  - `upsert_trade_episode`：trade_episodes（仓位级：trade_recorder 调用）
- 本 Port**逐字镜像这两个 helper 的签名与语义**（含内部吞错→False、
  POSTGRES_DSN 未配置→False），不发明 record_open/record_close 等业务接口
  （生产代码中没有这种形态，§七 禁止人为造接口）
- Port 只 persist already-produced data：不算 PnL、不解析 fill、不判 close
- 不暴露 connection/cursor/SQL；PG 角色为**历史台账（可选）**，非运行时
  source of truth（进程重启无 PG 恢复路径——见 inventory）
- adapter 由 orchestration/Persistence 侧 wiring（Phase 7），当前 NO-WIRING
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PositionLedgerPort(Protocol):
    """PG 台账最小能力端口（structural typing）。

    契约 = shared.postgres_client 两个 ledger 函数的签名逐字镜像：
    - record_trade_event(data) -> bool   （trade_events，幂等 DO NOTHING）
    - upsert_trade_episode(data) -> bool （trade_episodes，幂等 UPSERT）
    """

    def record_trade_event(self, data: dict) -> bool:
        """写入一条事件级台账记录。返回是否成功（未配置/失败=False，吞错）。"""
        ...

    def upsert_trade_episode(self, data: dict) -> bool:
        """幂等 UPSERT 一条仓位级完整交易记录。返回是否成功（同上）。"""
        ...
