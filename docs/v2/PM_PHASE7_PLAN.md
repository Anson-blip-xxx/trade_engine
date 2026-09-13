# PositionManager Phase 7 Decomposition Plan（P7-00）

> 基于 feature/v2-architecture @ `3e687a2`。本阶段 docs-only：把已有
> Inventory/Ports/Golden 全部串起来，给出"按什么顺序真正拆 1967 行 God Objects"
> 的可执行迁移计划。**Production 0 change。**

---

## 1. PM Baseline（真实量化）

| 项 | 值（cfac→3e687a2） |
|-----|---------------------|
| LOC | **1967**（shared/position_manager.py） |
| function/class | 61/1 |
| module-level mutable state | **9 组**（§16 表） |
| threads | 2 daemon（algo worker / WS kline）+ `_big_orders` 内嵌 2 daemon |
| queues | `_ALGO_QUEUE`（进程内存 FIFO + 11s 消费线程） |
| Redis access | `_rget/_rset/lock_*` + 直连 delete（OBS-5） |
| Binance access | `fapi_*`（经 P4-03-01-C 已接 ExecutionService 或扔 legacy light） |
| PG/CH | `record_trade_event` / CH insert（经 `_s6api` 锚） |
| file IO | 无（`pm:positions` 双写方为 se 直写） |
| notification | TG（requests 直连）/日志 |
| monitoring | `monitor_all`/`_monitor_one` 11 步退出链 |
| SE LOC | 1102（已有 Execution/decision/risk 委托） |

## 2. State Ownership（7 层真相图，不合并！）

```text
Binance positionRisk        ← Exchange 真相（唯一事实判断层）
      ↓ 三层加载（PM._load）
WS _WS_POSITIONS            ← 实时流缓存（leader 租约决定 connect）
      ↓
Redis pm:positions          ← 本地元数据层（position_id/algo_sl_id/tp_done 等 enrichment；
      ↘ 文件 pm_state.json    文件 fallback；**双写方** PM._save + se._update_pos_cache）
_POS_CACHE（se 进程内缓存） ← 防重复开仓快路径（写直通）
      ↓
PG trade_episodes/trade_events ← 历史台账（非运行时 SoT——D3 十问 #1）
CH trade_history/market_state_log ← analytics sink
```

- pm:positions **不是** 真相来源（RMW 缺口径 = P4-00/PM OBS-3）；真相=交易所。
- _POS_CACHE 双写问题（PMB-1/PMB-2）：two writers（`se._update_pos_cache` RMW
  直写 + `PM._save` 全量快照），**读写列表**：
  - Writers: `se._update_pos_cache`（试点单路径 D2 pilot 接入的是 PM._save，非此 RMW）
  - Readers: `PM._load` 三层 / `_ghost_cleanup` / `reconcile_all` / `WS leader close-flow`
  - Race risk: Redis 故障静默 + 双写竞争 = `pm:positions` 双写方在 P6-04 前没人统一。
  - **Consolidation Plan**：P7-02 PositionStateService 接 `_load_meta/_save` 与
    `se._update_pos_cache` RMW 合并为一个入入口（可复用 PositionStatePort）；迁移
    包完→删除 legacy alias（Phase 8 清理）。

## 3. closed marker（`closed:{sym}`）现状

- Set: `PM._mark_closed`（Close 开头）；Clear: `_clear_closed_marker`（失败后入无锁定）
- Check: `_was_closed_recently`（SE 重开冷却 + PM 防重入）
- Semantics: **无 Redis TTL**——4h 窗路由由 ts 比较实现（PMB-4
  "actually-lives-forever if not cleared due to failed re-open"；**resume-later**）
- Future owner：**CloseCoordination module**（轻量）。判断：**不需要新 Port**——
  保留现隐藏文件 `shared/position_manager.py` 内命名区域即可（sweep-in-scope 较小），
  为 Phase 7.2（PositionStateService）一并吸收（不单独立 port）。

## 4. Thread / Queue Ownership

| 线程/队列 | current owner | future owner | import-side-effect 边缘 |
|-----------|---------------|--------------|--------------------------|
| `_algo_worker_loop` 11s daemon | PM module import → `se`（`_algo_start_worker()` at se L355） | **ProtectionService**（Phase 7.4 拆出；由 orchestration 显式 start，禁止模块 import 布防） | P4 已有 import-side-effect tests（E-OBS-9 家族） reused |
| WS kline daemon（leader 進化） | PM（leader 租约决定连线） | **MonitorService** 一同搬 | 无新增 worker now |
| `_big_orders` analyze/spot_snapshot daemon | PM `_wsmarket brain loop` | MonitorService + LedgerService（敏感 trade_recorder RT 依赖） | 关键决策：monitor import-only 接线 |
| `monitor_all` 心跳 | S6/S8 主循环 | MonitorService（但外部 owner 由 se 前门提供） | import 不动 |

线程复用原则：**拆模块不能 spawn 多份 worker**；migration order 见 §14。

## 5. Algo Queue（→ ProtectionService 关键输入）

| 项 | 事实 |
|-----|------|
| producer | `se.open_position` success path / `PM.open_position` / `_update_stop_loss` / `_place_trail_sl`；**经 ProtectionPort/`_algo_enqueue` 独立打过** |
| consumer | `_algo_worker_loop` daemon 11s 节拍 |
| retry | **无** —— 失败(ATR)只 log（E-OBS-3 冻结） |
| persistence | 无（进程内存，重启丢） |
| restart | boot 无 purge——下次 open 正常继续；重启后丢失队列记录（11s 窗后 SL 缺失处） |
| 已有 Port 边界 | ProtectionPort（P4-03-01-D4 已 5 方法覆盖） |
| **Migration Phase** | **P7-04** ProtectionService 拿走 queue/worker（backend 完全相同——just relocation） |

## 6. Ownership Mapping（主要函数→未来 owner）

| Function/State | Current | Future owner | Ports used | Golden constraints |
|----------------|---------|--------------|------------|---------------------|
| `_load/_save/_load_meta/_merge_meta/pool` | PM module | **PositionStateService**（P7-02） | PositionStatePort | 三层加载序 WS→REST→meta；S0-5 稀释不可改 |
| `_update_pos_cache`(_POS_CACHE 写直通) | se | PositionStateService（P7-02） | PositionManagerPort | POS estimations 冻结（PMB-1/2） |
| `open/close/_partial_close` 编排 | PM | **PositionLifecycleService**（P7-03 之后的位置） | ExecutionService（P4-03-01-C 已接）+ PublisherPort | E-OBS-6a promote order 冻结 |
| `monitor_all/_monitor_one` | PM | **MonitorService**（P7-05） | 无新 port | 11 triggers 逻辑冻结 |
| `_ghost_cleanup/_try_record/reconcile` | PM | **ReconcileService**（P7-06） | LedgerPort（record_trade） | current ordering 冻结 |
| `_ledger/write_state/trade episodes/partial merge` | trade_recorder + PM | **LedgerService**（P7-03） | PositionLedgerPort | dual PnL 冻结 |
| `_algo_*` | PM | **ProtectionService**（P7-04） | ProtectionPort | PMB-9 fallback GET 冻结 |
| `_notify_external_position/_log_close_error` | PM | NotificationPort wrapper（P7-03/04） | NotificationPort | 内容/节流冻结 |
| `pm_monitor/_ws_*`/locks | PM | （迁移尾部）MonitorService + Coordination | — | ws leader 租约语义冻结 |

## 7. Current Dependency Graph

```text
strategies/s6.S6/S8.S8 ─┐
strategies/shared_executor ─┤
       │                     ├──> PositionManager (God)
                           ┌┴┐
                           │ │── fapi_*（分解自 legacy light）
                           │ │── _rget/_rset/locks（Redis）
                           │ │── record_trade_event / record_trade (PG+CH)
                           │ │── ExecutionService（P4 已接 close/partial/open order submission）
                           │ │── threading: algo worker + WS + locks
                           │ └── requests（TG 直连 / exchangeInfo）
                           │
       services/tv_bridge -→ （ Readonly 消费者）
       scripts/reconcile  -→
```

## 8. Target Dependency Graph

```text
Orchestration（shared_executor / S6/S8 / monitor caller）
      │
      ├─ ExecutionService ─► BinancePort ─► Adapter(SHARED)
      │
      └─ PositionManagerFacade（compatibility 保留）
            ├─ PositionStateService ─► PositionStatePort ─► Redis(+文件 fallback)
            ├─ LedgerService ─► PositionLedgerPort ─► PG/CH/trade_recorder
            ├─ ProtectionService ─► ProtectionPort ─► BinancePort(algo orders)
            ├─ MonitorService ─► StateService.Read / ExecutionService(last-resort only)
            ├─ ReconcileService ─► StateService + LedgerService
            └─ NotificationPort
Ports ↑ Adapters ↑，**禁止反向**。
```

## 9. Golden Behavior Mapping（行为→owner+phase）

| Golden ID | Current | Future owner | Phase |
|-----------|---------|--------------|-------|
| OBS-1 position_id 双格式 | open/_merge_meta | PositionStateService | P7-02（统一属修复项，**仍不修**） |
| OBS-2 signal_type 字段名 | open/_close | LifecycleService | P7-03 |
| OBS-3 same-sym 重复 open | open_position | LifecycleService | P7-03 |
| OBS-4 partial 无校验/negative qty | _partial_close | LifecycleService | P7-03（**冻结不修**） |
| OBS-5 `_clear_closed_marker` 直连 Redis | marker | CloseCoordination | P7-02 |
| OBS-6 sandbox `_close_position` key | sandbox | P6/P4 已隔离（不动） | — |
| OBS-7 qty=0 merge | _merge_meta | StateService | P7-02 |
| OBS-8 partial 无 record | _partial_close | Lifecycle+Ledger 边界 | P7-03 |
| OBS-10 静默吞错 | _save/_load_meta | StateService | P7-02 |
| PMB-1 双写方 pm:positions | se._update_pos_cache/PM._save | StateService | P7-02 |
| PMB-2 _POS_CACHE 写直通 | se | StateService | P7-02 |
| PMB-4 marker 无 TTL | _mark_closed | CloseCoordination（StateService 内隐含） | P7-02 |
| PMB-6 partial 无 ledger | _partial_close | Lifecycle+Ledger 边界 | P7-03 |
| PMB-7 双 PnL | PM vs recorder | LedgerService | P7-03 |
| PMB-9 fallback GET cancel | _algo_cancel | ProtectionService | P7-04 |
| PMB-8 cancel 覆盖矩阵 | _close/rejected 分支 | LifecycleService | P7-03 |
| E-OBS-3 11s 窗口 | worker | ProtectionService | P7-04 |
| E-OBS-13 TG 失败=已注册 | se.open_position | LifecycleService（非 PM） | P7-03 或**后续** ticket |

## 10. Ordering Constraints（不可打乱）

| Path | 冻结顺序 |
|------|----------|
| Open | gates → Binance order → PM state → PG → TG → algo enqueue |
| Close full | marker → risk#1 → order → risk#2 → cancel → pg → record → save（E-OBS-6a vs flat 分支 *reversed*） |
| Partial | order → qty 更新 → Redis save（**无 PG**——PMB-6） |
| Algo | exchangeInfo cache → cancel-all → place（PMB-8 矩阵） |

## 11. Failure Topology Mapping（swallow 语义归属）

每函数吞错语义原封不动给 owner：`_save/_load_meta/_mark_closed`→StateService；
`_algo_place_sl_inner{'error'}`/`_algo_cancel`→ProtectionService；
`_ghost/reconcile→continue`→ReconcileService；`_log_close_error`→Notification；
`_close` rejected→`_clear_closed_marker`→LifecycleService。
**禁止统一 exception strategy**。

## 12. Recommended P7 Sub-phases

| Phase | Content | 先修？ | 依据 |
|-------|---------|-------|------|
| P7-01 | PM Decomposition Golden Expansion（死角 characterization——补充 PM 绿基线） | docs+tests | 拆前先金化 |
| **P7-02** | StateService（含 CloseCoordination 收编 marker） | **第一个 net NEW production 代码** | 已有 PositionStatePort + seam 清晰（P4-D2） |
| **P7-03** | LedgerService（record_trade/trade episodes/CH partial merge） | P7-02 后 | LedgerPort/D3 就绪 |
| **P7-04** | ProtectionService（Algo queue/worker/_algo_*） | P7-02/03 后 | ProtectionPort/D4 就绪 |
| **P7-05** | MonitorService（monitor_all/_monitor_one/WS/locks） | P7-02 后 | 依赖 StateService |
| **P7-06** | ReconcileService（ghost/external） | P7-03 后 | 依赖 State+Ledger |
| **P7-07** | LifecycleService（open/close/partial编排——最高风险） | 最后 | 依赖全部前缝 |
| **P7-08** | PM Facade closure + legacy alias 清理（Phase 8 收尾） | 终 | 复验 E-OBS |

调整原因：LEGACY open/close 直接搬风险最高 → 前移 state/ledger/protection
这些 side-effect seam 已端口化且 goldens 密集的边界；lifecycle 收缩放最后。

## 13. Module Globals Migration Plan

| Global | Current | Future owner | Stage | alias? |
|--------|---------|--------------|-------|--------|
| `_个性化的 _last_api_call/_last_algo_update` | PM | ProtectionService | P7-04 | env-var fallback? legacy alias 至 Phase 8 |
| `_ALGO_QUEUE/_ALGO_QUEUE_LOCK/_ALGO_WORKER_STARTED` | PM | ProtectionService | P7-04 | alias 必需（se._algo_enqueue 兼容） |
| `_WS_POSITIONS/_WS_LAST_UPDATE/_WS_LOCK/_WS_STOP/_WS_INSTANCE` | PM | MonitorService | P7-05 | alias |
| `_monitor_heartbeat_ts/_RECENTLY_GHOSTED/_CLOSE_ERROR_LOG_TS` | PM | Monitor/Notification | P7-05 | alias |
| `_API_KEY/_API_SECRET/_ensure_apikey` | PM | （Phase 8 删——统一 BinancePort 已覆盖） | — | — |
| `_data_cache_mod` | PM | MonitorService（indirect） | P7-05 | — |
| `_breadth_symbols_cache` | s0 guard | StateService within S0（已完成） | — | — |

## 14. Phase 7 不修清单（所有 KNOWN FROZEN 维持）

S0-1 mismatch / duplicate open / partial no-reduceOnly / negative partial /
closed-marker no-TTL / dual PnL / PMB-9 fallback GET / 11s SL 窗 / TG=False-after-
success / Redis+PG dual state / position_id 双格式 / hidden exception swallowing /
EMA 双路径 / alts+shock wall-clock (S0-3) / breadth dilution（S0-5）。

## 15. Test Strategy per 子阶段

| 子阶段 | characterization | parity | guard |
|--------|------------------|--------|-------|
| P7-01 | 盲点（e.g. reconcile_all output contract / WS close-flow limit） | — | — |
| P7-02 | 镜像 `_load_meta/_save/_update_pos_cache` 全尾迹 | legacy vs service 同同输入 dict/引用 | import graph |
| P7-03 | record_trade/eptisodes 直现 | dual PnL 冻结、CH shapes | state-span 用例 |
| P7-04 | enqueue/worker loop 11s 节拍：复用 157 tests | PMB-9 fallback 冻结 | thread count ==1 |
| P7-05 | monitor_all triggers 级大厅 | lock/seconds 权重 | no-import-thread spawn |
| P7-07 | lifecycle 用例 | E-OBS-6a 顺序 | no PM coupling reversals |

复用 tests/position_manager/（74）+ tests/execution/（394）+ tests/s0（152）不重复造平行 test。

## 16. Top-10 Migration Risks

1. `_monitor_one` 依赖 SET_N state（global `_last_api_call` 等）—— 重排序会触发节流差异
2. `_ALGO_QUEUE` FIFO 全局语义：两个 worker 会破坏 11s 限速
3. WS Leader 租约：rpc threads Hold `_WS_LOCK`，不可并发刷新
4. `_load` 三层序：WS→REST→meta 不可打乱（reconcileAll 幽灵误判）
5. se 和 PM 两条 import-path（谁启动了 Worker）跨进程互踩
6. Better odd cases: `_try_record_ghost_trade` 在 lock 内做 record_trade —— LedgerService 不能再派生附加锁
7. P6/P4 中已迁移代码的 "double migration"（削 function and re-import）防退化
8. 幽灵仓 lock `pm:ghost_close:{sym}` key 名保持
9. `_clear_closed_marker` 直接连 redis（OBS-5）——已由 P4/PM 测试快照，撤散时若重声明严格 unknown
10. `record_trade` 依赖 income API —— 他可能在 close 记账中间调用 Binance；Ledger拆 需隔离

## 17. Which Phase First Moves Production Code

**P7-02 PositionStateService** = 第一个真正动生产代码的子阶段
（`_load/_save/_load_meta/_merge_meta/_update_pos_cache` 委托至 service）；
P7-01 是 tests-only。

## 18. Phase 8 Leftovers

- 删除 legacy alias（_ALGO_QUEUE 直访问等）
- `_ensure_apikey`/`_API_KEY` 清理（BinancePort 全替代后）
- reconcile/external 脚本接口 (`scripts/reconcile_binance_fills.py`)
- `_breadth_symbols_cache` globals 进一步收敛
- unique position_id/统一后决定是否 re-publish（政策 ticket）

*（同时含 lifecycle/BEHAVIOR 修复 tickets：S0-1 mismatch；duplicate open 前置等。）*

## 19. 结论
P7-00 完成后进入 P7-01（金色扩展），然后按序 P7-02 起**开始动生产代码**。
