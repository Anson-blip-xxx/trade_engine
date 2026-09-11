# Execution Phase 4 Closure（P4-03-01-D5 · 验收）

> 基于 feature/v2-architecture @ `0d9f451`，production diff **0**。
> 本文档回答唯一问题："Execution 层能否稳定冻结，让 Phase 5/6/7 在其上演进？"——**YES**。

---

## 1. 依赖方向审计（AST + 子进程，`test_phase4_architecture.py` 26 tests）

| 规则 | 结果 |
|------|------|
| `execution.core` 仅 stdlib（`__future__/dataclasses/enum/typing`） | ✅（AST 精确断言集合相等） |
| execution 全包无 `strategies/shared/redis/requests/psycopg/telegram/binance` | ✅（17 模块逐个 AST） |
| ports ↑ adapters（ports 不 import adapters） | ✅ |
| adapters 不 import service / ports | ✅ |
| service 不依赖 concrete PM | ✅ |
| `PM → execution` 单向（无反向） | ✅（无 ExecutionService→PM；position_state 经 PM 注入工厂为调用方→execution 方向） |
| import smoke：全包 exit 0 / 无线程 / 无 IO | ✅（子进程 before==after 线程数、IO 模块集==[]） |
| **S7 隔离** | ✅ services/s7 无任何 execution 依赖（AST guard；KEEP/DEFER 保持） |

## 2. 最终权威架构（真实状态，PM 未拆——Phase 7）

```text
S3 / TradingView
      ↓
S6 / S8
      ↓
Decision（decision/core）        Risk（risk/core + risk/service）
      ↓
shared_executor orchestration      （Decision/Risk gates + S6/S8 状态）
      ↓
ExecutionService ──► BinanceExecutionPort ──► SharedExecutorBinanceAdapter ──► Binance

Execution result
      ↓
PositionManager orchestration（God Object，明确保留到 Phase 7）
      ├── PositionManagerPort   register_opened_position       【D1 contract，未接】
      ├── PositionStatePort     load/save_positions(+_save pilot wiring) 【D2，写路径 pilot 已接】
      ├── PositionLedgerPort    record_trade_event / upsert_trade_episode 【D3 contract，未接】
      ├── ProtectionPort        enqueue/placer/cancel_algo…【D4 contract，未接】
      └── NotificationPort      notify_external_position / log_close_error 【D4 contract，未接】

PM 仍独占：monitor/reconcile/ghost/closed marker/线程与队列所有权/
保护状态写回/WS 三层加载/节流状态。
```

## 3. 跨边界序列冻结（`test_phase4_integration_closure.py` 22 tests）

- **Open**：`service.execute → pm_update → pg:OPEN_ORDER_FILLED`（固有序列）；
  TG 后 Algo enqueue 为尾副作用；Redis 先于 PG（E-OBS-2 原文顺序）
- **Close full**：`mark → risk#1 → port → risk#2 → cancel → pg(CLOSE_ORDER_FILLED) → record → save`
- **Close flat**：`mark → risk#1 → cancel(SL) → record → pg(FLAT)`（**record 在 pg 前**）
- **Close rejected**：不 cancel（保 SL）+ 清 marker + False；except：清 marker + False
- **Partial**：`port → qty → save`；**PG/ledger/mark/Algo 全部零写入**（PMB-6 冻结）
- **Protection**：payload 冻结（BOTH/CONDITIONAL/STOP_MARKET/MARK_PRICE/GTC/reduceOnly）；
  成功回写 `algo_sl_id`；失败 `{'error'}`/`{'code'}` 无写回；cancel 异常不阻断 close；
  **enqueue 失败 → open 仍 True**（trade continues）

## 4. Failure Matrix（全部 OBSERVED，不修）

| Failure Point | trade continues? | return | swallowed? | marker | Redis | PG | Algo | PM 内存 |
|---|---|---|---|---|---|---|---|---|
| Binance open order 失败/拒绝/异常 | No | False | 异常→外层 | 无 marker 逻辑 | 未写 | 未写 | 未入队 | 未注册 |
| Binance close order 失败 | Yes（等下轮） | False | 吞错记日志 | **清除** | 未变 | 未写 | 保留 | 保留 |
| PM update failure（open） | No | False | 外层 try | — | 尝试写 | 未写 | **尝试 cancel** | 不注册 |
| Redis save 失败（open/_save/close） | Yes | 不受影响 | 静默（file 降级） | — | 失败即无 | 不变 | 不变 | 内存可能领先 |
| PG event 失败 | Yes | bool 忽略 | 吞错→False | — | 不变 | 缺失 | 不变 | 一致 |
| trade_recorder（record_trade）失败 | Yes | 被吞 | 内部多段 try | — | 不变 | 缺 episode | 不变 | 一致 |
| Algo enqueue 失败（open） | Yes | **True** | 吞错日志 | — | 已写 | 已写 | **仅日志** | 不变 |
| Algo place 失败（worker） | Yes（异步） | `{'error'}` | 吞错 | — | id 不写回 | 不变 | **无 SL（11s 窗口+失败）** | 不变 |
| Telegram 失败（open） | 交易实际已成 | **False**（E-OBS-13 新冻结） | 外层捕获 | — | 已写 | 已写 | 已入队 | **已注册** |
| positionRisk 失败（open gate） | Yes（fail-open） | 继续执行 | 吞错 | — | — | — | — | — |
| positionRisk 失败（close） | No | False | 外层捕获 | **清除** | 未变 | 未写 | 保留 | 保留 |

## 5. Known Frozen（Phase 4 不修清单——全部 KNOWN/FROZEN）

duplicate open after submission（E-OBS-2/PM OBS-3）· MARKET cancel 无效性（E-OBS-1/2）·
Algo 11s 无保护窗口（E-OBS-3）· partial 无 reduceOnly（E-OBS-5）· negative partial（E-OBS-7）·
closed marker 无 TTL（PMB-4）· Redis/PG 双状态 · PG 非 real SoT（D3 十问 #1）·
partial 无 ledger（PMB-6）· 双 PnL 口径（PMB-7）· **Algo cancel fallback GET**（PMB-9）·
四套 Binance 实现（E-OBS-10/N3）· S7 独立事务 · **PM God Object** ·
**TG 失败 → False 但已注册**（E-OBS-13，本阶段新冻结）· `_POS_CACHE` 模块全局 identity（N6）。

## 6. Phase 7 TODO（承接点）

1. PM decomposition：state/persistence/ledger/monitoring/protection 切片（D1-D4 contract 就绪）
2. 双写方统一（PMB-1）；`closed:` TTL 语义显式化（PMB-4）；partial reduceOnly 决策（E-OBS-5）；
   PnL 口径统一（PMB-7）；algo cancel fallback 修复（PMB-9）；重复开仓前置（E-OBS-2/PM OBS-3）
3. se 断舍离：`_algo_start_worker` import 副作用、四套 Binance 实现统一、`_s6api` 双语义
4. ports 未接者逐 pilot：PositionManagerPort → PositionStatePort(_load_meta/RMW) → Ledger/Protection/Notification

## 7. Exit Criteria（全部满足）

[1] 984 passed full　[2] 394 execution　[3] 生产 diff 0　[4] core 纯（AST 集合断言）
[5] ports 独立（无 adapters/PM/SE 依赖）　[6] 无循环导入（全包 smoke）
[7] open 序列冻结　[8] close 序列冻结（full/flat/rejected/except/remaining）
[9] partial 冻结（PMB-6 零 PG）　[10] protection 冻结（payload/写回/失败吞错/cancel 不阻断）
[11] 失败矩阵 10 项　[12] Golden index 成文　[13] S7 未触碰　[14] Phase 7 TODO 成文
