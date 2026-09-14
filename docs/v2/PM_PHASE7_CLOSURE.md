# PM Phase 7 Closure — PositionManager Decomposition（P7-08）

> 基于 feature/v2-architecture @ `abdca14`。**生产代码零改动**（仅有 tests + docs）。
> 回答一个问题：State / Ledger / Protection / Monitoring / Reconcile /
> Lifecycle 是否已建立独立 ownership，同时历史交易行为是否被架构迁移
> 意外改变？答案：**是 → Phase 7 PASS / CLOSED**。

---

## 1. Phase 7 Scope（P7-00 定案 → 逐里程碑交付）

把 shared/position_manager.py（1967 行，61 函数 / 9 组模块级可变状态 /
2 大 daemon 线程 + `_big_orders` 内嵌）按"golden 先行、行为保真、服务独立"
次序拆成六个具备独立 ownership 的服务。

## 2. Before Architecture

```text
PositionManager (God Module, 1967 行)
├── state           ── pm:positions load/save、closed marker、三层 load 链、merge
├── ledger          ── record_trade / PG episode / CH insert / TG 推送 / income 对账
├── protection      ── _algo_enqueue / worker / _algo_place_sl_inner / _algo_cancel / _cancel_all_algo
├── monitoring      ── monitor_all / _monitor_one 11 步退出链 / WS 快照 / ws:leader lease / ghost Step0 调用
├── reconcile       ── reconcile_all / ghost cleanup 记账 / external-position alert / migrate
├── lifecycle       ── open_position / _close / close_position / _partial_close
├── runtime globals ── _WS_* / _RECENTLY_GHOSTED / _ALGO_QUEUE / marker backing / _CLOSE_ERROR_LOG_TS
├── threads         ── algo worker (11s 循环) / WS connect loop / _big_orders 内嵌
└── wrappers        ──（无 —— 全部内联）
```

## 3. After Architecture

```text
PositionManager (compatibility facade / runtime shell, 1296 行)
├── compatibility facade   ── open/close/partial/monitor/reconcile/ghost/migrate 全 thin delegate
├── factories（晚绑定）     ── _state_service / _position_state / _protection_service /
│                            _monitoring_service / _reconcile_service / _lifecycle_service /
│                            _execution_service
├── runtime globals        ── _WS_POSITIONS/_WS_LOCK/_WS_LAST_UPDATE/_WS_STOP/_WS_LEASE_*/_WS_INSTANCE/
│                            _RECENTLY_GHOSTED/_ALGO_QUEUE/_ALGO_WORKER_STARTED/_CLOSE_ERROR_LOG_TS/
│                            _last_algo_update/_last_api_call/SYSTEM_CFG/_SYSTEM_KEYS
├── legacy aliases         ── marker/_load/_load_meta/_save/_get_cfg/_position_id 等工具保持
└── glitch 保留段          ── marker helpers / 三层 load 链（ws_snapshot/rest/merge）/ noop cooldown
        ↓ delegate（零行为差异）
PositionStateService(122L)      ← P7-02
PositionLedgerService(346L)     ← P7-03B
PositionProtectionService(64L)  ← P7-04B
PositionMonitoringService(425L) ← P7-05B
PositionReconcileService(304L)  ← P7-06B
PositionLifecycleService(383L)  ← P7-07B

ExecutionService / Risk / Decision / S0 / S3 / journal / replay —— 维持独立（Phase 4~6）
```

## 4. Ownership Matrix

| Responsibility | Before | After Owner | PM Role |
|---|---|---|---|
| State（pm:positions load/save + 三层链 assembly） | PM inline | PositionStateService + Port adapter | wrapper `_load/_save` |
| Closed marker（set/check/clear，无 TTL ts 窗） | PM inline | PositionStateService | wrapper `_mark_closed` |
| Ledger（record_trade settlement 编排） | trade_recorder | PositionLedgerService（P7-03B） | thin |
| Income 对账 / dual PnL | trade_recorder inline | LedgerService（PMB-11/12 保留） | thin |
| Protection（Algo SL place/cancel/queue） | PM inline | PositionProtectionService | thin `_algo_*` |
| Algo queue（FIFO list + 11s worker） | PM inline | PM 侧 runtime + ProtectionService | backing 保留 |
| Monitoring（monitor_all/_monitor_one/WS/lease） | PM inline | PositionMonitoringService | thin `monitor_all/_monitor_one/_ws_*` |
| Ghost / reconcile / external alert / migrate | PM inline | PositionReconcileService | thin wrappers |
| Open / Full close / Partial / Flat lifecycle | PM inline | PositionLifecycleService | thin（`close_position→close_fn` seam） |
| Runtime global backing（全部 12 组） | PM | **PM 唯一 backing**（services 经注入引用同一对象） | owner |
| Thread spawn（algo worker / WS loop） | PM | **PM 入口唯一触发**（S6A import-time worker = N1 冻结） | owner |
| Compatibility API + 签名 | 内联实现 | PM facade（26 个 name 全数保留，签名不变） | facade |

## 5. Dependency Direction Audit — PASS

`tests/position_manager/test_phase7_architecture_closure.py`（39 tests）AST seal：
- 六个 service 模块 **零** `position_manager` / `shared_executor` import
- 无 sibling concrete import（OWNERS 集合互斥）
- 顶层 import 无 `redis_store / binance_api / requests / websocket`
- service 源码无 `Thread(` 启动（方法体内 frozen lazy seam 除外：websocket / scripts.sandbox）

## 6. Circular Import Audit — PASS

subprocess 清导：`position_state / position_ledger / position_protection /
position_monitoring / position_reconcile / position_lifecycle /
shared.position_manager` 全程零 crash / 零 circular（`PM_NO_WS=1`）。

## 7. Import Side-Effect Audit

| 模块 | Thread | Redis | Binance | sleep | HTTP |
|---|---|---|---|---|---|
| position_state/ledger/protection/reconcile/lifecycle service | ✗ | ✗ | ✗ | ✗ | ✗ |
| position_monitoring service | ✗ | ✗（惰性 lock_owner 调用时） | ✗ | ✗ | ✗ |
| shared.position_manager | `PM_NO_WS=1` guard WS 线程；`se._algo_start_worker()` import-time（N1 冻结）| 惰性 | 惰性 | — | 惰性 |
| strategies.shared_executor | import 启动 worker（N1 冻结） | 惰性 | 惰性 | ✗ | ✗ |

## 8. Runtime Single-owner Audit — PASS

identity guard 锁定：`service.gq is pm._RECENTLY_GHOSTED`、
`svc.wsp is pm._WS_POSITIONS`、`svc.wsl is pm._WS_LOCK`、
`svc.lkey==pm._WS_LEASE_KEY=='ws:leader'`、TTL==45、
`_CLOSE_ERROR_LOG_TS`/`marker`/`_ALGO_QUEUE`（ProtectionService 经 `enqueue_fn=_algo_enqueue` 单 backing）。
**零 shadow state。**

## 9. Compatibility Facade Audit — PASS（26 API 全数存在，签名保持）

见 parametrize 列表 + signature 测试（`open_position` 6 位置参数、
`system=''`/`margin_type='CROSSED'` 默认、`_close` 的 `force` kw 等）。

## 10. Cross-Service Integration Matrix（P7-08 对应 9 smoke，全绿）

| 链 | 对应 Golden / smoke |
|---|---|
| Open：guard→Execution(杠杆/保证金/订单)→Protection(ENQUEUE)→State(save) | test_open_golden + open success smoke |
| Duplicate open：order+SL 前置→skip→True | input golden + duplicate smoke（PMB-30） |
| Full close：mark→risk#1→Execution→risk#2→Protection cancel→Ledger→PG→State | test_lifecycle_ordering_golden + full smoke |
| Exchange-flat：risk#1→Protection→Ledger→PG FLAT→State | close golden + flat smoke（无 exec 断言） |
| Partial：Execution→State（0 ledger / 无 cancel / 无 marker） | edge-case golden + partial smoke |
| Partial-fill close：State→REC(final=F)→PG PARTIAL | close golden（PMB-28） |
| Monitor：State(load)→decision(11 步)→Lifecycle(close_fn) | monitor_one golden + smoke |
| Reconcile：Exchange/Local compare→silent channel | reconcile goldens + smoke（PMB-24） |
| WS：快照→record→marker→meta_filtered 丢弃 | ws golden + dedup smoke（PMB-21） |

## 11. Lifecycle Side-effect Matrix Replay — 16/16 PASS

PM_LIFECYCLE_GOLDEN_INDEX 16 行 matrix 逐行对 facade→service 路径重放
（lifecycle_callers/失败/排序三大 golden 直接覆盖），**无 expected 修改**。
PASS count **16/16**。

## 12. Golden Family Final Ledger（索引）

| ID | Area | Status | Owner | Future ticket |
|---|---|---|---|---|
| OBS-1~10 | PM 内inhibits（ID 双格式 / signal_type 缺位 / 重复 open 等） | FROZEN | Phase 8 ticket | OBS-3 dup 前移 ticket |
| PMB-1/2/11 | dual writer / _POS_CACHE write-through | FROZEN | P7-08 保留 | Phase 8 consolidation |
| PMB-4 | marker 无 TTL（ts 窗） | FROZEN | StateService | resume-later |
| PMB-6/13 | partial 无 ledger 全链缺失 | FROZEN | LedgerService | behavior ticket |
| PMB-10 | ghost 保留旧 SL（部分成交不 cancel） | FROZEN | LifecycleService | 11s gap ticket |
| PMB-11/12 | dual PnL / income 内嵌 | FROZEN | LedgerService | dual-PnL ticket |
| PMB-14/15 | inline exchangeInfo / cancel-all 状态过滤 | FROZEN | ProtectionService | activity ticket |
| PMB-16~22 | 心跳节流/ghost side 缺失/延期一轮/全量保存/lease fail-open/record→mark/1h阻断 | FROZEN | MonitoringService | PMB-17 修复 ticket |
| PMB-23~26 | pop-before-record / silent reconcile / dead queue / marker 自愈+TG/PG 不对称 | FROZEN | ReconcileService | ticket |
| PMB-27~30 | partial marker 保留 / save 序不对称 / noop cooldown / dup-check 前置孤立 SL | FROZEN | LifecycleService | ticket |
| E-OBS-1~13 | Execution 各档（fill rate/reduceOnly/fallback GET/11s 窗口/TG-false-after-success） | FROZEN | ExecutionService | Phase 8 tickets |
| S0-1 ~ S0-12 | S0 冻结事实（含 S0-1 field mismatch fail-open） | FROZEN | S0 Regime Engine | S0-1 ticket |

## 13. Known Frozen Defects（KNOWN / NOT FIXED）

duplicate open 后置查重 · dup-check 前孤立 SL · 负数部分平仓增仓 ·
marker-first close · partial 0 ledger · dual PnL 口径 · PMB-9 fallback GET ·
11s 无 SL 窗口 · ghost side=None 消费 · pop-before-record · silent reconcile ·
dead recently-ghosted runtime · partial marker persistence · save 序不对称 ·
noop cooldown · S0-1 market_state/market_mode fail-open —— **全部保持**。本系统
仍为非事务系统（无 rollback/compensation）。

## 14. Remaining PM Responsibilities（可接受）

factories（7 个 late-bound factories）/ runtime globals（12 组）/ 26 个 legacy
wrappers + 签名保持 / marker helpers / 三层 load 链与 merge glue /
`_set_cooldown` noop / thread spawn glue。
**判定：可接受** —— 主体 ownership 已全部迁出；上述仅为 shell，Phase 8 清理。

## 15. LOC Migration Ledger

| 模块 | Phase 7 start | P7-08 now |
|---|---|---|
| shared/position_manager.py | ~1967（P7-00 基准 cfac→3e687a2） | **1296** |
| position_state/service.py | — | 122 |
| position_ledger/service.py | — | 346 |
| position_protection/service.py | — | 64 |
| position_monitoring/service.py | — | 425 |
| position_reconcile/service.py | — | 304 |
| position_lifecycle/service.py | — | 383 |

服务合计 +1640；PM -671（wrappers/factories/globals 预期内保留）。

## 16. Production Diff Audit — 本 commit 生产 0 diff

`shared/position_manager.py` / position_* 各包 / execution/ /
strategies/shared_executor.py —— 全部为空（仅新增 tests/ 与 docs/）。

## 17. Git History（Phase 7 全 13 commits，git 校验一致）

P7-00 `a27850f` · P7-01 `0b15448` · P7-02 `1d54200` · P7-03A `9a4de4b` ·
P7-03B `9136779` · P7-04A `5b5c9d8` · P7-04B `67fc01b` · P7-05A `96ba840` ·
P7-05B `c251728` · P7-06A `5eab3f9` · P7-06B `0d380fc` · P7-07A `111a811` ·
P7-07B `abdca14`（间隔含 master hotfix `4c72dd4`——不属于 Phase 7）。

## 18. Test Regression（closure 时点）

- tests/position_manager：**436 → 484**（+39 arch + 9 integration）
- tests/execution：**394 passed**
- full：**1736 passed + 1 skipped**
- known warnings：`test_state_rmw_golden.py` PytestReturnNotNoneWarning
（pre-existing；tests-only 说明：该测试直接 `return` bool 断言值，
不改 expected；保留）

## 19. Phase 8 Candidates（只建议，不实施）

**Cleanup 类（无行为变更）**：1. compatibility alias 收敛；
2. 大构造器（37/41/22 注入位）分组结构；3. request/context dataclass；
4. 查重/`_load` 与 wrappers 合并策略；15. thread ownership 收敛；
16. runtime globals owner 化。
**Behavior-change ticket 类**：5. duplicate open 前置查重（连带 PMB-30 孤立 SL）；
partial ledger 补齐；负数 partial 校验；PMB-9 cancel（GET 充 delete 槽位）；11s SL gap；
PMB-17 ghost side；pop-before-record 修正；runtime adoption policy；
cooldown 真实现；S0-1 market_state/market_mode 修正；Position ID 统一（OBS-1/2）。
**两类必须分票处理。**

## 20. Final Verdict

**Phase 7 PASS / CLOSED。**
原定目标（State/Ledger/Protection/Monitoring/Reconcile/Lifecycle 六大
ownership 独立 + 全部历史交易行为保真）达成并有 golden/guard 双线证据。
