# Phase 8 Closure — Architecture Cleanup（P8-07）

> @ `5481502`。**生产代码零改动**（closure commit 仅 tests+docs）。
>
> **CLOSED ≠ every backlog item implemented.**
> 而是：all items resolved as either **COMPLETED** or **EXPLICITLY DEFERRED**.

---

## 一、Final Status Matrix

| ID | Item | Status | Commit | Reason | Risk | Next |
|---|---|---|---|---|---|---|
| C1 Dual-track `_light_fapi_*` IO consolidation | **DEFERRED / YELLOW** | — | transport 与黄金联动（protection/execution/reconcile 每点需等值证明，D2-pilot 级） | MID | Phase 9 A 项 |
| C2 Auth glue | **CLOSED** | `eda8868` | key parse/header/presence 迁 leaf；call-time + 缓存语义 == HEAD | GREEN | — |
| C3 Constructor grouping | **CLOSED** | B1/B2/B3/05C | Lifecycle/Reconcile/Monitoring → 5 bundles；Execution/Protection/State DELIBERATELY UNCHANGED | GREEN | — |
| C4 State glue | **CLOSED** | `68e8388` | `_load` chain thin + REST parse 迁 StateService | GREEN | — |
| C5 Funding helper | **CLOSED** | `f821d33` | pure parse/to-0 拓扑 leaf；transport 留 PM | GREEN | C1 收敛对手位 |
| C6 exchangeInfo cache | **DEFERRED / YELLOW** | — | 需行为级 cache/TTL ticket（PMB-14 交互） | YELLOW | Phase 9 A 项 |
| C7 Thread/runtime glue | **CLOSED** | B=`5481502` | **thread glue 迁 `position_runtime/`；backing deliberately remains PM-owned** | GREEN | — |
| C8 Static constants | **CLOSED** | `de18975` | 7 Green 常量 literal relocation；6 Yellow runtime 残留 PM | GREEN | — |
| C9 Context/request dataclass | **DEFERRED** | — | 依赖-Bundle dataclass **≠** 业务 request/context dataclass（不变义；若推进需行为票） | DESIGN | Phase 9 B |
| C10 Test dedup | **DEFERRED / OPTIONAL** | — | tests-only hygiene，无行为影响 | LOW | optional |

Phase 8 完成 = **all Green completed** + **remaining Yellow explicitly deferred**
+ behavior tickets isolated + boundaries tests green。

## 二、Final Architecture Snapshot

```text
PositionManager(1227 行/compatibility facade)
    ↓ delegate（零行为差异）
PositionStateService(148)  PositionLedgerService(346)
PositionProtectionService(64)  PositionMonitoringService(549)
PositionReconcileService(~350)  PositionLifecycleService(387)
ExecutionService / Risk / S0 / S3 / Decision / journal / replay

leaf 辅助：
position_config/constants.py（7 Green 常量）
position_market/funding.py（read_funding_rate pure parse）
position_market/auth.py（load_api_keys / build_api_header / has_credentials）
position_runtime/runtime.py（thread/start mechanics only）
```

## 三、Constructor Final State

| Service | Deps | 结果 |
|---|---|---|
| ExecutionService | 1 | NO CHANGE |
| ProtectionService | 5 | NO CHANGE |
| StateService | 5 | NO CHANGE |
| LifecycleService | 21 loose → **5 bundles** | runtime/execution/state/protection/action |
| ReconcileService | 22 loose → **5 bundles** | runtime/state/coordination/notification/action |
| MonitoringService | 40 loose → **5 bundles** | runtime/state/market/action/ws |

## 四、Runtime Final State

- **Thread mechanics**：`position_runtime/runtime.py`
- **Backing ownership（PM owns）**：`_ALGO_QUEUE`/`_ALGO_QUEUE_LOCK`/`_ALGO_WORKER_STARTED`/
  `_WS_POSITIONS`/`_WS_LOCK`/`_WS_LAST_UPDATE`/`_WS_THREAD`/
  `_monitor_heartbeat_ts`/`_RECENTLY_GHOSTED`/`_CLOSE_ERROR_LOG_TS`/
  `_last_algo_update`/`_last_api_call`
- **_POS_CACHE**：explicitly deferred（dual-writer cross-module）

## 五、Leaf Modules Final State

四个 leaf 全部满足：**clean import / no reverse PM import / no thread+HTTP+Redis side effect**
（P8-01/03/04/06B guard：AST import-name 断言）。

## 六、Compatibility Contract（Phase 8 全部保留）

- legacy PM wrappers（26 API）继续存在（签名不变，1200+ 测试证明）
- legacy PM symbols（常量/常量段）
- legacy monkeypatch seams（wrapper late-binding：`pm._get_funding_rate`、`pm._algo_*`、
  `pm._ws_on_open`、`pm._ws_connect_loop` 等）
- legacy runtime backing identity（`_WS_POSITIONS`/`_WS_LOCK`/`_RECENTLY_GHOSTED`/
  queue/worker flag/heartbeat dict 同一对象）
- legacy startup hooks（N1 frozen SE import-time algo worker；PM_NO_WS → WS boot）

Phase 8 做 compatibility break = 0。

## 七、Behavior Preservation Summary

Phase 8 不改变：订单/关闭/部分平仓/risk 阈值/monitor priority/
reconcile asymmetry/worker retry/worker sleep/WS leader/S0 行为。
每步迁移以 Summary severity 未变化、exit criteria 全绿确认。

## 八、Frozen Behavior Tickets

仍冻结（详 Migration constraint）：
**PMB-9**（fallback GET）
**PMB-17**（ghost side=None）
**PMB-23**（pop-before-record）
**PMB-24**（silent reconcile 通道）
**PMB-26**（TG/PG 吞错不对称）
**PMB-27**（partial marker persist）
**PMB-29**（noop cooldown）
**PMB-30**（dup-check 前置 孤立 SL）
**S0-1**（market_state/market_mode fail-open）
Inventory 行为票：**T1**（duplicate-open precheck）、**T5**（11s SL window）、
**T12**（save-order symmetry）、**T14**（依赖 T1）

## 九、Risk Classification Final

| 颜色 | 全联络 | 项 |
|---|---|---|
| **Green completed** | 6 | C2, C3, C4, C5, C7, C8 |
| **Yellow deferred** | 2 | C1, C6 |
| **Behavior/design deferred** | — | C9, C10 + 10 票 PMB/S0/T |

## 十、Commit Timeline（Phase 8 全 12 commits）

| 里程碑 | Commit | 类型 |
|---|---|---|
| P8-00 | `1f289e2` | docs |
| P8-01 | `de18975` | **refactor（constants）** |
| P8-02 | `68e8388` | **refactor（state glue）** |
| P8-03 | `f821d33` | **refactor（funding helper）** |
| P8-04 | `eda8868` | **refactor（auth glue）** |
| P8-05A | `f239dcd` | tests/docs |
| P8-05B1 | `20b7dbb` | **refactor（Lifecycle bundles）** |
| P8-05B2 | `cfd9824` | **refactor（Reconcile bundles）** |
| P8-05B3 | `59c5064` | **refactor（Monitoring bundles）** |
| P8-05C | `d467103` | docs/tests closure |
| P8-06A | `71baea9` | tests/docs |
| P8-06B | `5481502` | **refactor（runtime glue）** |

## 十一、Deferred Guards

- C1：`_light_fapi_get/post/delete` 仍在 PM legacy owner（AST production diff
  `HEAD~1` 无主体）——**not silently migrated**
- C6：exchangeInfo cache 未变（仍 inline，PM 头部）
- C9：无 new business request/context layer（`DEFAULT_*_DEPS`/Request dataclass
  未引入）
- C10：tests dedup 0 执行（optional 类）

## 十二、Phase 9 Candidate Roadmap

**A（Yellow architecture/IO）**：C1 dual-track IO consolidation、C6 exchangeInfo cache
**B（Behavior tickets）**：
S0-1 · PMB-9 · PMB-17 · PMB-23 · PMB-27 · PMB-29 · duplicate-open precheck ·
11s SL window · save-order symmetry · position-id unification

**Handoff 原则**：Phase 8 closure 不指定修复顺序；Phase 9 先重新排序
风险/收益，先 Behavior Ticket Prioritization / Golden Review。

## 十三、Definition of Done

Phase 8 CLOSED 同时满足：
1. all Green cleanup completed；
2. all remaining Yellow items explicitly deferred；
3. behavior tickets isolated；
4. architecture boundaries tested；
5. full suite green；
6. no unresolved accidental production diff；
7. closure artifact complete（本文件）。
