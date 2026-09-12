# trade_engine V2 架构迁移 — Codex 接手文档

> 目标：让 Codex 无需重新梳理历史对话即可直接接手后续 V2 架构迁移。
>
> 仓库：`Anson-blip-xxx/trade_engine`  
> 工作分支：`feature/v2-architecture`
>
> **当前已完成的最新阶段：P6-01（S0 Golden Characterization）**
>
> **当前已确认的最新 commit：`974e8f2`**
>
> 下一阶段：**P6-02 — S0 Regime Classifier Core Extraction**
>
> 重要：不要假设后续阶段已经完成。以仓库当前 HEAD 为准重新核对。

---

## 1. 项目目标

这是一个多策略自动交易系统的 V2 架构迁移。

当前主目标不是“优化策略收益”，而是：

1. 把历史 God Module 逐步拆成明确边界；
2. 先用 Golden / Characterization Tests 锁死现有行为；
3. 再做小步抽取；
4. 每一步保持生产行为不变；
5. 把真正的行为修复留给后续独立 ticket。

核心原则：

> **Behavior Preservation > Architecture Purity**

不要在架构迁移阶段“顺手修 bug”。

---

# 2. 总体架构

当前交易链大致为：

```text
S3 / TradingView
      ↓
Unified Signal
      ↓
S6 / S8
      ↓
Decision
      ↓
Risk
      ↓
shared_executor orchestration
      ↓
ExecutionService
      ↓
BinancePort
      ↓
Binance
      ↓
PositionManager
      ├── PositionManagerPort
      ├── PositionStatePort
      ├── PositionLedgerPort
      ├── ProtectionPort
      └── NotificationPort
```

当前：

- S3 已完成 Phase 5 架构收口；
- Execution 已完成 Phase 4 收口；
- S0 正在 Phase 6；
- PositionManager 仍是 God Object，真正拆分留到 Phase 7；
- S7 继续 `KEEP / DEFER`，不要提前改。

---

# 3. 固定 Roadmap

后续不要因为局部建议漂移路线。

```text
Phase 0   Code Audit                     ✅ CLOSED
Phase 1   Journal / Replay / PM Golden   ✅ CLOSED
Phase 2   Unified Signal                 ✅ CLOSED
Phase 3   Decision / Risk                ✅ CLOSED
Phase 4   Execution Architecture         ✅ CLOSED
Phase 5   S3 Event Architecture          ✅ CLOSED
Phase 6   S0 Regime Engine               ← CURRENT
Phase 7   Position Manager Decomposition
Phase 8   Legacy Cleanup
```

---

# 4. 已完成阶段摘要

## Phase 0 — Audit

已完成代码审计，核心发现：

- `shared_executor.py` 是共享决策 / 执行大模块；
- `shared/position_manager.py` 约 1940 行 / 59 functions；
- 存在多套 Binance API；
- 多套 sandbox guard；
- `shared_executor` vs `strategies.shared_executor` 存在 Python module identity 风险；
- Redis + JSON 双持久化；
- PM 的 load / merge / close 早期测试不足；
- `strategies/config/` 存在运行态文件；
- S7 是独立架构，KEEP/DEFER。

---

## Phase 1 — Journal / Replay / PM Golden

### P1-01 Decision Journal

核心：

- `journal/models.py`
- `journal/serializer.py`
- `journal/schema.py`
- tests

Journal 是被动 observer，不参与决策。

### P1-02 S6/S8 side-channel Journal

要求：

- fail-open；
- 不重新计算 gate；
- 不改策略行为；
- 不让 Journal 失败阻塞交易。

### P1-03 Replay

当前 Replay 是：

> Recorded Journal Replay / Comparison

不是 Strategy Re-execution Replay。

### P1-04 PM Golden Tests

冻结了多个 PM 历史行为：

1. `position_id` 有两种格式；
2. `signal_type/event_type` 语义不统一；
3. duplicate open 先发 Binance order，后检查已有 PM position；
4. negative partial close 可增加 qty；
5. `_clear_closed_marker()` 隐藏 Redis 依赖；
6. sandbox PnL 行为；
7. zero qty merge；
8. partial close 可能无 ledger；
9. `_close` 先标 closed，再真实 close；
10. Redis exception 被静默吞掉。

不要在架构迁移阶段修这些。

---

# 5. Phase 2 — Unified Signal

关键 commit：

- `c2dbe8a`
- `79e89588a45d47d48c8516db66b1698b42efa13b`

结构：

```text
signals/
  models.py
  enums.py
  normalize.py
  adapters.py
  validation.py
```

`Signal` 字段：

- symbol
- side
- signal_type
- source
- strategy
- strength
- timestamp
- event_id
- metadata

注意：

- S3 原始事件无 side；
- S6 注入 LONG；
- S8 注入 SHORT；
- `contract_score` 不属于 Signal；
- adapter strict，fail-open 放在 S6/S8 integration boundary。

---

# 6. Phase 3 — Decision / Risk

## Decision

已冻结：

- S6/S8 gate chain；
- `contract_score`；
- `open_position()` 内第二层 gate；
- 各种 current oddities。

不要“修正” unreachable combination / funding cache 等。

## Risk

### P3-04 已完成 Risk Core + Risk Service

commit：

`192515508d58558c8c34909a2d8b5cd9f326353c`

结构：

```text
risk/core.py
risk/service.py
```

纯函数：

- `score_to_fraction`
- `leverage_for_score`
- `bounded_stop_pct`
- `AtrRiskPositionSizer`

service：

- `calc_position_qty`
- drawdown state machine

重要 Frozen behavior：

1. min_notional 可覆盖 risk cap；
2. PUMP leverage 固定 2；
3. dual pool calculation；
4. malformed peak 触发 AttributeError；
5. loss_lock retry cycle。

不要在抽架构时修。

---

# 7. Phase 4 — Execution Architecture（已 CLOSED）

最终 closure commit：

`147477b`

最终当时：

- `tests/execution`: 394 passed
- 全量：984 passed

之后 Phase 5/6 又继续增加测试，所以不要使用旧数字作为当前基线。

## 7.1 Execution Core

commit：

`cfac25c`

文件：

```text
execution/core.py
```

纯逻辑包括：

- `OrderIntent`
- open/close side mapping
- open / close / partial intents
- result parsing
- fill classification
- qty kernel
- close PnL math
- sandbox predicate

Frozen asymmetry：

- SE open: MARKET + RESULT，无 positionSide/reduceOnly
- PM legacy open: MARKET + BOTH，无 RESULT
- full close: MARKET + BOTH + reduceOnly=true
- partial close: MARKET + BOTH，无 reduceOnly

注意：

`is_rejected()` annotation 声称 bool，但真实可能返回 `-2019` / `None`；测试已冻结，不要顺手修。

---

## 7.2 Execution Service

已建立：

```text
ExecutionService
  ↓
BinancePort
  ↓
Adapter
```

Open：

```text
S6/S8
 → shared_executor
 → ExecutionService
 → BinancePort
```

Close/Partial：

```text
PM
 → ExecutionService
 → BinancePort
```

---

## 7.3 PositionManager Boundary

commit：

`617f2b3`

新增：

- `execution/ports/pm.py`
- `execution/adapters/pm.py`

当前没有把 ExecutionService 强行 wiring 到 PM。

这是有意设计，避免：

```text
ExecutionService ↔ PositionManager
```

循环依赖。

---

## 7.4 Position State / Redis Boundary

commit：

`faf95f6`

Port：

```text
load_positions() -> dict
save_positions(dict) -> None
```

只处理：

`pm:positions`

`pm:positions` 定位：

> 本地 position metadata，不是 exchange source of truth。

真实仓位真相仍是：

> Binance `positionRisk`

`PM._save` 是唯一 pilot wiring。

不要提前统一：

- `_POS_CACHE`
- Redis
- PG
- closed marker
- exchange state

---

## 7.5 Ledger Boundary

commit：

`1636a6e`

Port：

```text
record_trade_event(data) -> bool
upsert_trade_episode(data) -> bool
```

PG 当前实际角色：

> 可选历史台账，不是仓位实时 SoT。

重要：

- restart 不从 PG 恢复；
- PG 写失败不阻止交易；
- partial close 当前无 PG ledger；
- full close 有 event / episode / CH 多套链；
- PnL 存在双口径：
  - event 级公式
  - episode 级 income 对账

不要在 Phase 7 之前统一。

---

## 7.6 Protection / Notification Boundary

commit：

`0d9f451`

Port：

Protection：

- `enqueue_algo_sl`
- `start_algo_worker`
- `place_algo_sl`
- `cancel_algo_id`
- `cancel_all_algo`

Notification：

- `notify_external_position`
- `log_close_error`

重要 Frozen：

### PMB-9

`_s6api` fallback tuple 第 3 槽实际上是 `_light_fapi_get`。

结果：

> `_algo_cancel` fallback 模式可能对 algoOrder 发 GET 而不是 DELETE。

可能导致取消静默失败、旧 SL 残留。

这是重大 bug，但当前已冻结。

不要在架构迁移阶段修。

---

## 7.7 Phase 4 Golden defects

至少包括：

- duplicate open 先下 Binance order 再检查 PM duplicate；
- MARKET cancel 实际可能无效；
- Algo SL 有约 11 秒 no-SL window；
- partial close 无 reduceOnly；
- negative partial close 可增加 qty；
- closed marker 无 Redis TTL；
- Redis/PG 双状态；
- PG 非 SoT；
- partial close 无 ledger；
- 双 PnL 语义；
- PMB-9 fallback GET bug；
- multiple Binance implementations；
- PM 仍 God Object；
- E-OBS-13：TG 失败时 `open_position=False`，但订单/PM/PG 已经成功。

全部 `KNOWN / FROZEN`。

---

# 8. Phase 5 — S3 Event Architecture（已 CLOSED）

最终 commit：

`54deb21`

Phase 5 最终架构：

```text
Market Input
   ↓
s3.core
   ↓
s3.detector
   ↓
Lifecycle / State
   ↓
S3RedisPublisher
   ↓
Redis
```

## 8.1 S3 Inventory

commit：

`6460ac3`

S3 真实入口：

```bash
python strategies/s3_orderflow.py
```

`run()` 启动：

- market_brain_loop daemon
- big-order WS daemon
- kline 1m WS daemon
- 以及内嵌辅助 daemon

---

## 8.2 S3 Golden Tests

commit：

`4512654`

已冻结 13 类 event family：

- PULSE_UP
- PULSE_DOWN
- PANIC_SELL
- VIOLENT_BULLISH
- VIOLENT_BEARISH
- PUMP_UP
- PUMP_DOWN
- TREND_UP
- TREND_DOWN
- HIGH_VOL
- LOW_VOL
- ATR_EXPAND
- FAILED_BREAKOUT
- 以及 END lifecycle

---

## 8.3 S3 Feature Core

commit：

`0e1bfe4`

文件：

```text
s3/core.py
```

函数：

- `ema`
- `rsi`
- `atr`
- `build_window_features`

重要：

- EMA full path 与 incremental path 有差异；
- `_ema_cache` 未迁移；
- 不要统一两条 EMA 路径。

---

## 8.4 S3 State Boundary

commit：

`626d682`

文件：

```text
s3/state.py
```

State：

- `EventStateStore`
- `BreakoutStateStore`

重要：

- `get()` 返回原引用，不 copy；
- dict insertion order 是行为；
- `_update_event_state` 会原地修改 evt，并返回同一引用。

这是：

> S3-8

后续不要因为“纯函数化”改变 identity semantics。

---

## 8.5 S3 Detector

commit：

`9fcd58e`

文件：

```text
s3/detector.py
```

API：

```python
detect_candidate_events(
    symbol,
    windows,
    windows_raw,
    log_fn=None,
    breakout_runner=None
) -> list[dict]
```

仍用 dict，不引入 CandidateEvent dataclass。

原因：

dict 结构本身已成为行为契约。

---

## 8.6 S3 Publisher

commit：

`54deb21`

`S3RedisPublisher`

接口：

- `write_event_snapshot(events, ts)`
- `write_market_snapshot(symbols, ts)`

重要：

`event:s3` 是：

> latest slot overwrite

不是 queue / stream。

即使没有 event：

> 仍写空 snapshot。

publish：

- channel: `s3:event:notify`
- payload: `'1'`
- 只有非空 event 时 publish

---

# 9. S3 Frozen Behaviors

当前至少有：

### S3-1
`event:s3` 是 latest-slot overwrite，不是 queue。

### S3-2
429 retry 递归且无明确上限。

### S3-3
`close_pos` 没写入 window，导致 `_strong_breakout` 正常链路不可达，`breakout_confirmed` 恒 False。

### S3-4
dedup/cooldown 是 process-local，多实例会重复事件。

### S3-5
per-event 无独立 timestamp。

### S3-6
module lifecycle state 重启即丢。

### S3-7
HIGH_VOL 实际 strength 下限不是 0，而是约束后 30。

### S3-8
lifecycle helper 原地突变 evt + 返回同一引用。

### S3-9
detector 源码顺序与 Redis 输出层 `sort(-strength)` 是两层不同 ordering。

### S3-10
VIOLENT 方向独立于 breakout / close_pos。

### S3-11
Publisher 单 try 拓扑：
- set 失败 → publish 跳过；
- publish 失败 → market snapshot 仍可继续写。

全部 frozen。

---

# 10. Phase 6 — S0 Regime Engine（CURRENT）

## 10.1 P6-00 Inventory

commit：

`ccf7e87`

S0 入口：

```text
services/s0/s0_market_guard.py
```

约 337 行。

独立进程：

```bash
python services/s0/s0_market_guard.py
```

特点：

- 无线程；
- 无 leader；
- 30s 采样；
- 60s 写节流。

链路：

```text
main
  ↓
sample_btc
  ↓
sample_breadth
  ↓
compute_state
  ↓
write_state
  ├── Redis
  ├── atomic file
  └── ClickHouse
```

S0 强依赖 S3：

> 主要读取 `market:s3_data`

---

## 10.2 Breadth

当前 universe：

- top50
- 6h cache
- 排 BTC
- 排稳定币

bullish 计数条件：

```text
close > ema20 > 0
```

重要：

> S3 数据缺失的 symbol 仍计入 denominator，但不计入 numerator。

所以 breadth 会被缺数据稀释。

这是当前行为，不能顺手修。

---

## 10.3 Bull/Bear

bull：

```text
ema20 > ema60
AND
price > ema20
```

bear：

```text
ema20 < ema60
AND
price < ema20
```

否则 neutral。

全是 strict inequality。

---

## 10.4 Regime

`market_state`：

- trend
- range
- risk-off

`regime`：

- bull_trend
- weak_bull
- range
- weak_bear
- risk-off

当前没有 SOFT / HARD risk-off。

risk-off 是单一布尔/单一档。

---

# 11. S0 最重要 Frozen Behaviors

P6-01 commit：

`974e8f2`

当前专项：

- `tests/s0`: 55 passed
- 全量：1234 passed + 1 skipped

## S0-1 — 极重要

Producer 写：

```text
market_state
```

但：

`shared_executor.market_allows_trading`

读：

```text
market_mode
```

结果：

> S6/S8 risk-off gate 当前实际上 fail-open。

这是重大缺陷。

**不要在 Phase 6 classifier 抽取时修。**

后续应该单独 behavior-change ticket。

---

## S0-2

`is_system_allowed`

缺关键字段时：

> 默认 True

即 fail-open。

---

## S0-3

`alts_sync / shock`

受 wall-clock modulo gate 控制。

相同市场输入：

> 因当前时间不同，可产生不同结果。

这意味着 S0 production 当前不是完全 deterministic。

后续 pure classifier 可以 deterministic，但 wrapper 必须保留原时钟语义。

---

## S0-4

empty breadth pool：

> 默认 0.5

可能制造“normal”假象。

---

## S0-5

missing S3 symbols：

> 稀释 breadth denominator。

---

## S0-6

risk-off：

> 无 SOFT/HARD。

---

## S0-7

S0 classifier 当前：

> stateless

无 hysteresis、无 transition state。

restart 后直接重算。

---

## S0-8

多实例：

> 无协调，last-writer-wins。

同时 CH 可能重复写。

---

## S0-9

`write_state` 三写异常语义不一致：

```text
Redis → atomic file → CH
```

当前：

- Redis failure：吞；
- file failure：上抛；
- CH failure：warning，不抛。

不要统一。

---

## S0-10

多个核心阈值是 strict inequality。

例如：

- amp > 0.04
- breadth_ratio < 0.30
- strong > 0.7
- weak_bear < 0.35
- atr expand: vol15 > vol24 * 1.3

等号语义已经被 Golden Tests 冻结。

---

# 12. 当前真实开发位置

当前最新已完成：

```text
P6-01 S0 Golden Characterization ✅
commit: 974e8f2
```

**下一阶段：P6-02 S0 Regime Classifier Core Extraction**

还未确认完成。

Codex 接手后第一件事：

```bash
git status
git branch --show-current
git log --oneline -10
pytest -q tests/s0
pytest -q
```

确认实际 HEAD 与测试基线。

不要仅依赖本文档。

---

# 13. P6-02 明确任务

目标：

从：

```text
services/s0/s0_market_guard.py
```

抽出：

```text
s0/core.py
```

用于：

```text
sampled metrics/context
        ↓
pure classifier
        ↓
market_state / regime / permit / score / trend_strength
```

## 应抽

`compute_state()` 中纯分类逻辑。

## 不应抽

- sample_btc
- sample_breadth
- top50 refresh
- Redis
- atomic file
- ClickHouse
- Binance/ticker fetch
- S3 Redis read
- main loop
- sleep cadence
- wall-clock sampling ownership

---

# 14. P6-02 Core 约束

`s0/core.py`：

- stdlib-only；
- 无 Redis；
- 无 requests；
- 无 CH；
- 无 file IO；
- 无 thread；
- 无 sleep；
- 不自己读当前时间。

wall-clock gate 如果 currently 在 `compute_state`：

> 应由 wrapper/orchestration 计算后作为输入传入 core。

这样 core deterministic，但 production 行为保持不变。

---

# 15. P6-02 必须保持

以下全部不变：

- market_state
- regime
- permit
- score
- trend_strength
- breadth
- bull/bear
- risk-off
- contradictory zone
- strict thresholds
- output types
- S0-1
- S0-2
- S0-3
- S0-9

特别：

> 不得把 `market_state` 改成 `market_mode`。

哪怕这是明显 bug。

---

# 16. P6-02 推荐测试

新增：

```text
tests/s0/test_regime_core.py
tests/s0/test_regime_core_parity.py
tests/s0/test_regime_core_architecture.py
```

至少覆盖：

- strong bull
- weak bull
- range
- weak bear
- risk-off
- bull + ratio 0.32 contradictory zone
- breadth weak overlap
- empty/default-like input
- atr expanding
- neutral

要求：

legacy `compute_state`
vs
new core

同输入：

> dict 全等。

---

# 17. P6-02 Exit Criteria

只有全部满足才 PASS：

1. pure classifier core established；
2. regime parity 100%；
3. threshold parity 100%；
4. risk-off parity 100%；
5. contradictory zone preserved；
6. type semantics preserved；
7. legacy API preserved；
8. S0-1 unchanged；
9. wall-clock behavior unchanged；
10. Redis/file/CH unchanged；
11. tests/s0 全绿；
12. full suite 全绿。

建议 commit：

```text
refactor(v2): extract S0 regime classifier
```

---

# 18. Phase 6 后续固定路线

P6-02 后：

```text
P6-03 Publisher Boundary
P6-04 Input / Snapshot Boundary
P6-05 Integration Closure
```

## P6-03

处理：

```text
Redis → atomic file → ClickHouse
```

重点镜像 S0-9 三种不同 exception semantics。

不要统一。

## P6-04

处理：

- S3 market snapshot input
- BTC sample
- breadth sample
- sentiment
- ticker
- top50 pool cache

但不要改变采样逻辑。

## P6-05

类似 Phase 4/5 closure：

- architecture guards
- end-to-end regime path
- consumer compatibility
- failure matrix
- golden index
- full suite
- production diff 尽量 0

然后：

> Phase 6 CLOSED

---

# 19. Phase 7 — PM Decomposition 预备信息

Phase 7 不要从头设计。

已有直接输入：

- `docs/v2/PM_BOUNDARY_INVENTORY.md`
- `docs/v2/PM_GOLDEN_OBSERVATIONS.md`
- `docs/v2/REDIS_BOUNDARY_INVENTORY.md`
- `docs/v2/LEDGER_BOUNDARY_INVENTORY.md`
- `docs/v2/PROTECTION_MONITORING_INVENTORY.md`
- `docs/v2/EXECUTION_PHASE4_CLOSURE.md`
- `docs/v2/PHASE4_GOLDEN_BEHAVIOR_INDEX.md`

已有 ports：

- PositionManagerPort
- PositionStatePort
- PositionLedgerPort
- ProtectionPort
- NotificationPort

Phase 7 不应重新发明这些边界。

应该：

> 用这些已有 contract 去逐步把 PM 内部职责搬出去。

---

# 20. Phase 7 仍需拆的 PM 职责

至少包括：

- `_load_meta`
- `se._update_pos_cache` RMW 双写方
- closed marker 三函数
- `_monitor_one`
- `monitor_all`
- ghost/reconcile
- WS 线程
- leader lease
- algo worker
- `_ALGO_QUEUE`
- notification throttling
- external position handling
- `_s6api`
- legacy `PM.open_position`
- file fallback
- Redis/PG/CH side effects
- partial/full close orchestration

不要一次全拆。

仍然坚持：

> characterization → boundary → pilot → closure

---

# 21. Phase 8 — Cleanup

Phase 8 才考虑：

- legacy wrappers removal
- duplicate Binance API consolidation
- deprecated paths
- dead fields
- stale docs
- module identity cleanup
- known naming inconsistencies

但：

只有在 Phase 7 完成、Golden Tests 足够覆盖后进行。

---

# 22. 永远不要提前改的行为

除非进入明确 behavior-change ticket，否则不要修：

### Execution / PM
- duplicate open after Binance submission
- MARKET cancel ineffective
- 11s Algo SL window
- partial close no reduceOnly
- negative partial close behavior
- closed marker no TTL
- dual Redis/PG state
- dual PnL
- PMB-9 fallback GET
- TG failure returns False after order already succeeded

### S3
- latest-slot event:s3
- empty event snapshot write
- 429 unbounded recursive retry
- breakout unreachable
- process-local dedup
- no per-event ts
- restart loses lifecycle
- mutable aliasing
- detector vs Redis ordering split
- EMA incremental/full difference

### S0
- market_state/market_mode mismatch
- fail-open
- empty breadth=0.5
- missing-symbol dilution
- no SOFT/HARD
- wall-clock mod nondeterminism
- last-writer-wins
- Redis/file/CH different failure semantics

---

# 23. Coding Strategy

每个阶段都遵循：

```text
Inventory
  ↓
Golden / Characterization Tests
  ↓
Pure Core / Boundary
  ↓
Minimal Pilot Wiring
  ↓
Parity
  ↓
Integration Closure
```

禁止：

```text
“大重构”
→ 再补测试
```

---

# 24. Testing Rules

每一步必须：

```bash
pytest -q <专项目录>
pytest -q
```

不要只跑新增测试。

已有 Golden Tests 是行为契约。

如果新实现导致旧 Golden Test 失败：

> 默认认为 production behavior 被改坏。

不要直接修改旧 expected。

先分析。

---

# 25. Diff Rules

每一步提交前：

```bash
git status --short
git diff --stat
git diff
```

检查：

- 是否越界；
- 是否意外修改 S7；
- 是否修改已 CLOSED phase；
- 是否顺手改 thresholds；
- 是否修改 Redis key/value/TTL；
- 是否改变异常吞噬；
- 是否改变调用顺序。

---

# 26. Commit 风格

建议：

```text
docs(v2): inventory ...
test(v2): characterize ...
refactor(v2): extract ...
refactor(v2): establish ...
test(v2): close ...
```

每一步小 commit。

不要把多个 phase 混进一个 commit。

---

# 27. Architecture Guard 原则

纯模块尽量做到：

- stdlib-only；
- import 无副作用；
- 无网络；
- 无 Redis；
- 无 DB；
- 无线程；
- 无文件写。

已有阶段大量使用：

- AST source guard
- subprocess clean import
- fake clock
- fake Redis
- fake Binance
- spy call-order

继续沿用。

---

# 28. S7 规则

S7：

> KEEP / DEFER

不要让 S7 强制依赖：

- 新 ExecutionService
- 新 S3 core
- 新 S0 core

除非单独启动 S7 migration phase。

Phase 4 已有 source guard 确保 S7 未被新 ExecutionService 强绑。

---

# 29. 关键文档清单

Codex 接手后优先阅读：

```text
docs/v2/EXECUTION_INVENTORY.md
docs/v2/EXECUTION_GOLDEN_OBSERVATIONS.md
docs/v2/EXECUTION_PHASE4_CLOSURE.md
docs/v2/PHASE4_GOLDEN_BEHAVIOR_INDEX.md

docs/v2/PM_BOUNDARY_INVENTORY.md
docs/v2/PM_GOLDEN_OBSERVATIONS.md
docs/v2/REDIS_BOUNDARY_INVENTORY.md
docs/v2/LEDGER_BOUNDARY_INVENTORY.md
docs/v2/PROTECTION_MONITORING_INVENTORY.md

docs/v2/S3_INVENTORY.md
docs/v2/S3_GOLDEN_OBSERVATIONS.md
docs/v2/S3_STATE_BOUNDARY.md
docs/v2/S3_PHASE5_CLOSURE.md
docs/v2/PHASE5_GOLDEN_BEHAVIOR_INDEX.md

docs/v2/S0_INVENTORY.md
docs/v2/S0_GOLDEN_OBSERVATIONS.md
```

---

# 30. 接手后的第一组命令

建议 Codex 首先执行：

```bash
git status --short
git branch --show-current
git log --oneline -20

pytest -q tests/s0
pytest -q

find docs/v2 -maxdepth 1 -type f | sort
find tests/s0 -maxdepth 2 -type f | sort
```

然后阅读：

```text
services/s0/s0_market_guard.py
docs/v2/S0_INVENTORY.md
docs/v2/S0_GOLDEN_OBSERVATIONS.md
tests/s0/
```

确认实际 HEAD 后再开始 P6-02。

---

# 31. Codex 当前应执行的任务

## P6-02 — Regime Classifier Core Extraction

一句话目标：

> 把 `compute_state()` 中“已采样市场上下文 → regime”的纯分类逻辑抽到 `s0/core.py`，保持 production behavior 100% 不变。

要求：

- core deterministic；
- wrapper 保持 wall-clock current behavior；
- S0-1 继续 fail-open；
- strict thresholds 不变；
- contradictory zone 不变；
- write_state 零修改；
- consumer 零修改；
- 全量测试绿。

完成后汇报：

1. commit hash
2. commit message
3. changed files
4. production diff
5. core API
6. legacy wrapper
7. parity matrix
8. thresholds parity
9. S0-1 status
10. S0-3 status
11. S0-9 status
12. tests/s0
13. full pytest
14. next recommendation

---

# 32. 最终原则

整个 V2 迁移过程中，最重要的不是“代码越来越优雅”。

最重要的是：

> 在交易系统仍然可以真实运行的情况下，
> 把历史隐式行为逐步显式化，
> 用测试把它们锁住，
> 再一点一点建立清晰的 architecture boundary。

任何“明显不合理”的行为，只要已经存在于生产链：

> 先冻结，后单独修。

不要把“架构迁移”和“策略/行为优化”混在一起。
