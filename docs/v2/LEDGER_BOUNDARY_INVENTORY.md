# Ledger Boundary Inventory（Phase 4-03-01-D3）

> 基于 feature/v2-architecture @ `faf95f6` 的实际代码逐行核实。
> 识别并隔离"交易台账/持久化记录"职责：inventory → contract → adapter →
> characterization tests → **NO-WIRING（写入逻辑分散+异常吞噬，按 §九 不接）**。

---

## 1. PG helper 面（shared/postgres_client.py · 129 行 · 未改动）

| helper | 表 | SQL 形态 | 返回 | 幂等 |
|--------|-----|----------|------|------|
| `record_trade_event(data: dict) -> bool` | `trade_events` | INSERT ... ON CONFLICT (event_id) DO NOTHING | True/False | 幂等（DO NOTHING） |
| `upsert_trade_episode(data: dict) -> bool` | `trade_episodes` | INSERT ... ON CONFLICT (position_id) DO UPDATE SET ... | True/False | 幂等（UPSERT） |

- 两者共同语义：`enabled()` False（无 `POSTGRES_DSN`/显式关闭）→ **立即 False，
  不连接**；序列化在 helper 内（metadata/payload → `json.dumps(default=str)`，
  env 兜底 `_data_env()`）；每写一次独立连接 `_connection()`：
  **成功 commit / 异常 rollback+raise / finally close**，外层 except → False
  （**helper 永不向调用方抛错**）
- 环境开关：`POSTGRES_DSN` + `POSTGRES_ENABLED`（env / config/binance.env）；
  `get_env()`（demo/prod）由 binance.testnet 决定并落 `env` 字段
- 无其他运行时 DB 访问：`psycopg` 只出现在 postgres_client / `scripts/init_postgres.py`
  （建表）/ `scripts/report_performance.py`（只读报表）

## 2. 十问十答（全部依据真实代码）

| # | 问题 | 答案 |
|---|------|------|
| 1 | PG 是 Position source of truth 吗？ | **不是**。文件 docstring 自称 "transactional source of truth"，但**代码无任何 PG 读回/恢复路径**；运行时真相 = Exchange positionRisk。宣称与行为不符（OBSERVED） |
| 2 | 还是历史 ledger？ | **是**——trade_episodes（仓位级完整记录，UPSERT）+ trade_events（事件级流水，APPEND）；PG 唯一角色 |
| 3 | 还是 operational state？ | 否 |
| 4 | Exchange vs PG 冲突谁优先？ | **Exchange 永远优先**：对账/幽灵流全部以 positionRisk 为准，PG 从不参与判定 |
| 5 | Redis vs PG 冲突谁优先？ | **Redis**：所有持仓决策读 `pm:positions` 三层加载（WS→REST→meta），PG 不在任何读路径 |
| 6 | 重启后从 PG 恢复仓位？ | **否**——`PM._load` 只有 WS/REST/meta 三层；init_postgres.py 仅建表 |
| 7 | PG 参与实时交易判断？ | **否**——全部调用点不检查 bool 返回值（fire-and-forget） |
| 8 | 写失败阻止交易？ | **否**（helper 吞错 → False，调用方忽略） |
| 9 | 写失败影响 PM 内存状态？ | **否**（先后已成的事实：pop/save 已执行或未执行均不回滚） |
| 10 | 写失败影响 Redis？ | **否**（零交叉；trade_recorder 内 Redis partial 合并独立于 PG 结果） |

## 3. 全量 DB 使用分类（A-E）

| 类 | 内容 | 位置 | 进入 PositionLedgerPort |
|-----|------|------|--------|
| **A. Position Ledger** | trade_episodes（仓位级：入场/出场/qty/pnl/时长/exit_reason/score/sl/ghost…） | `trade_recorder.record_trade` → `upsert_trade_episode` | **是**（seam 2） |
| **B. Trade/Execution Ledger** | trade_events（事件级：OPEN_ORDER_FILLED / CLOSE_ORDER_PARTIAL/FILLED / EXCHANGE_POSITION_FLAT / REALIZED_PNL(se 侧无) / EXTERNAL_POSITION_DETECTED / reconcile script） | se L1011、PM._close 三分支、`_notify_external_position`、scripts/reconcile_binance_fills.py → `record_trade_event` | **是**（seam 1；事件 payload 含 Binance order_id/executedQty/avgPrice） |
| C. Strategy/Signal Logging | enqueue_closed_trade 异步分析（文件/CH）、journal（另链路） | trade_recorder 尾部 | 否（不入 Port） |
| D. Runtime/Ops | health、reconcile 脚本报表 | scripts | 否 |
| E. Other | ClickHouse `_ch_insert('default.trade_history')` —— **不同数据库**，与 PG 平行的 analytics sink | trade_recorder L296 | 否（本阶段不建 CH port；仅记录） |

## 4. Open / Partial / Full Close 的 DB 写入真实顺序（逐行核实）

### Open（se.open_position，生产顺序）
```text
Binance order(Service → Port)
  → parse/classify(core) → 取消分支(部分<50% 等)
  → PM state：_update_pos_cache → Redis pm:positions(+文件) + _POS_CACHE   ← PM state 先
  → PG trade_events(OPEN_ORDER_FILLED)   ← PG 在 PM state 之后、TG 之前（唯一 PG 写）
  → TG 开仓通知
  → Algo enqueue
```
注意：**open 不产生 trade_episodes 行**；也无 record_trade 调用（开仓不写仓位台账）。

### Partial Close（PM._partial_close）
```text
closed marker?  → 无（不防重入）
  → Binance order(经 Service，无 reduceOnly)
  → qty/exit自我更新 + Redis _save（无位置确认 secondRisk）
  → PnL：仅 _pmlog（pnl_u 公式，core.remaining_after_partial 同站增量）
  → **PG：无任何写入**；record_trade：无；TG：无；Algo cancel：无
```
→ **partial close 无 ledger 记录**（OBSERVED，PMB-6；不补）。

### Full Close（实盘成交分支，真实顺序）
```text
mark_closed(Redis closed:sym)  ← 先标记
  → positionRisk#1 确认 → order(Service) → positionRisk#2 剩余判定
  → _cancel_all_algo（CANCEL 在 record/PG 之前）
  → PG trade_events(CLOSE_ORDER_FILLED)      ← 第 1 个 PG 写（事件级，realized_pnl=公式值）
  → record_trade(final=True)（trade_recorder）→ Redis partial 合并
     → Binance income API 对账（如可用）→ **PG trade_episodes UPSERT**（第 2 个 PG 写）
     → ClickHouse trade_history
     → loss cooldown(Redis) → analysis 异步
  → positions.pop + Redis _save
```
保底：DB 写失败（False）不回滚已执行的 pop/save/cancel——事件顺序事实（不补偿）。
Exchange-already-flat 分支：cancel → record → pg(EXCHANGE_POSITION_FLAT)（record 在 pg 前，
两分支相反——E-OBS-6a）。沙盘分支：无 Binance、record_trade 内仍走 PG/CH。

## 5. PnL 盘点（只读观察项）

| 项 | 事实 |
|-----|------|
| PnL produced by | ① 纯公式核：`execution.core.position_pnl/remaining_after_partial/partial_pnl_u`（P4-02，仅排序/日志/PG 事件字段用）；② **对账版**：`trade_recorder.record_trade`（公式值起步 → Binance `/fapi/v1/income` REALIZED_PNL 非零覆盖 → Redis `trade:partial:{sha1}` 的 partial 分段合并（qty 加权 exit 价）） |
| PnL persisted by | ① PM._close 的 `CLOSE_ORDER_FILLED/PARTIAL` pg 事件 `realized_pnl`（**即时公式值**）；② trade_episodes `pnl_usdt/pnl_pct`（**income 对账+合并值**）；③ ClickHouse `trade_history` 行 |
| PnL consumed by | `enqueue_closed_trade`(trade_analyzer 异步分析)、`scripts/report_performance.py` |
| 已知问题 | **同一平仓两处 PnL 口径不同**（事件级=PM 即时公式 vs episode 级=Income 对账）——OBSERVED（PMB-7），**不修**；fee：income 单 REALIZED_PNL 不含 fee 分解（fapi income 含 commission 差异口径，事实记录） |

## 6. PositionLedgerPort / PostgresLedgerAdapter

```text
orchestration（se/PM/trade_recorder，Phase 7 前）
    ↓ wiring（当前未接）
execution.ports.ledger.PositionLedgerPort
    ↓
execution.adapters.postgres_ledger.PostgresLedgerAdapter（callable 注入）
    ↓
existing PG helper（shared.postgres_client：连接/SQL/序列化/吞错原样）
```

- **接口 = 两个 helper 签名逐字镜像**（不发明 record_open/partial/close 业务口）
- 不暴露 connection/cursor/SQL；不算/不重载 PnL；不判 close 来源
- 依赖规则：port/adapters 无 psycopg/redis/strategies import（测试守护）
- **wiring 决策：NO-WIRING**——满足 §九 全部"不接"特征（写入分散 7 处、
  trade_recorder 与 Redis partial 合并/CH/loss cooldown 强耦合、helper 自身
  吞错返回 bool 属调用方忽略模式）。No-wiring 状态有测试冻结。

## 7. 未迁移 DB 职责（Phase 7）

7 个 `record_trade_event` 调用点（se 1、PM 4、reconcile 脚本 1향）、
`upsert_trade_episode`（trade_recorder）、ClickHouse 链、income 对账、
Redis `trade:partial:*` 合并、loss cooldown、analysis enqueue。

## 8. Phase 7 注意事项

- unified PnL 口径决策（事件级 vs episode 级）必须显式（现两套并存）
- partial close 无 ledger 是行为缺口——补录属行为变更，需显式决策
- `_connection` per-write 无连接池（每写一次 connect/commit/close）——
  迁移时保持行为，性能属另一显式决策
- `trade_episodes` UPSERT 以 position_id 为幂等键：PM OBS-1（ID 双格式）
  直接影响台账主键一致性
