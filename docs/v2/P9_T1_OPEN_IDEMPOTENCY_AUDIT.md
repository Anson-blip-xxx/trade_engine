# Phase 9-03A — T1 / PMB-30 Open Idempotency Semantic Audit
# （docs+tests only；production 0 diff）

> @ `e3f1427`。在移动 duplicate check 之前，先回答“重复开仓是什么”。
> 未定义语义下前移 precheck，只是把一个 bug 变成另一个业务语义 bug。

---

## 一、当前真实 open 顺序（frozen @ f1d3cd2）

**Strategy 层（S6/S8 同构）**：
strength gate → analysis gate → R:R gate → drawdown gate →
closed-marker(4h) gate → PAUSE_OPEN 文件 gate → notional gate →
market_allows_trading gate → existing-position gate
（`has_any_position(symbol)`：local `_pm_load` + exchange positionRisk）→
calc_position_qty → `open_position(...)`

**PM 层（`position_lifecycle.open_position`）**：
closed-marker guard → leverage POST → marginType POST → real order POST →
`_algo_start_worker()` + `_algo_enqueue(sl)`（PMB-30 前置）→ build position dict →
`_load()` → duplicate check `if symbol in positions → log+return True`（OBS-3）→
save → log → True

## 二、Duplicate 定义（真实实现盘点）

| 通道 | Key | Owner |
|---|---|---|
| S6/S8 策略层 gate | symbol-any-side（one-way mode 刻意语义） | `has_any_position`（local+exchange 双看） |
| system-level cache | symbol+system（strict prefix） | `has_position(name)` |
| PM lifecycle layer | symbol only | dup-check `symbol in positions` |
| exchange 层 | positionRisk positionAmt（local-missing truth source 但只在策略层） | ghost/reconcile |
| idempotency key / request key | 不存在 | — |

## 三、产品语义问题（未定义 → NEEDS_PRODUCT_DECISION）

SE strategy gating means：same symbol+side local existing → 策略层已经 reject
（existing_position journal action）。PM lifecycle dup-check 防御意义；
实际 production 调用一般不会进到 PM dup 分支（策略层先挡）。

未定义：
- same symbol、opposite side（S6 LONG 持仓 + S8 SHORT 请求）→
  `has_any_position` side-blind 一律 reject —— 当前已锁定 one-way mode 语义，
  但没有 manifest（NEEDS_PRODUCT_DECISION）
- 同 symbol 不同 system（S6 LONG BTC + S8 SHORT BTC）→ one-way 模式下 both blocked
  （side-blind 一次只允许一个 side）；NEEDS_PRODUCT_DECISION（当前隐含 REJECT，非显式策略）
- closed-marker 4h gate 是否算 duplicate 的一部分（当前已冻结 4h 阻塞 pre-reopen
  在 strategy 层 pre-gate，作用域外）

## 四、Cross-system 语义

- S6 LONG 持仓 + S8 同 symbol SHORT：`has_any_position` side-blind → S8 reject
  → 双向不并存（one-way mode）
- 无 side-based key —— 决定 duplicate key 不能用 symbol+side，PM 层 dup-check 用
  symbol（保持）

## 五、position_id relation

- position_id 在 real order 之后 build dic 时生成；不可作为下单前 idempotency key
- OBS-1/2 双格式未统一（POS-ID deferred）
- T1 precheck 不应强依赖 position_id（symbol-key precheck）

## 六、Local vs Exchange duplicate

- Local duplicate：`symbol in _load()` —— precheck 可移前
- Exchange-only duplicate（local 无记录 / exchange 已持仓）：
  策略层已覆盖（`has_any_position` 会 fapi_get positionRisk）；PM lifecycle 层
  无 exchange precheck —— T1 只能在 lifecycle 层做 local-only precheck；
  全局幂等需策略层+PM 层双支撑（当前已具备策略层）
- 结论：T1 precheck 只需 PM 内防护，非全局幂等承诺

## 七、Retry Semantics（save 失败后 retry）

- retry same request → local state 可能 empty → 重复 real order +
  duplicate SL enqueue（PMB-30 变体路径）
- 关键 observation：save-failure 比 normal dup 更危险，修复属
  与 T12 save-order 同根（非 T1 单独可修）
- exchange reality reconcile 属 ghost/reconcile 域

## 八、PMB-30 Relation

- 当前 dup-check 后置 → real order+enqueue 已发生 → PMB-30 孤立 SL
- T1 precheck 前移 = PMB-30 自然消失（同一 fix 收益）
- 但 orphan SL 不只有 dup-open 路径：4h closed-marker blocking（策略层）已在
  PM 管道外层发生 → 无 SL；save 失败后 retry、ghost 清理顺序 —— 仍在 PMB-30 域
- ⇒ T1 解决 PMB-30 的 dup-open ordering 分支；不合并票，记录 dependency

## 九、Concurrency / Race

- 两 open 同时达：都先 local 空 → 都真实 order
- 现有 lock 仅 `pm.monitor.writer`(30s)/`pm:ghost_close:{sym}`(60s)；
  无 symbol+side open-scoped lock
- 结论（硬冻结）：单纯 precheck 不等于 concurrency-safe idempotency
- 拆票：T1-A sequential/single-process precheck ordering（低风险）；
  T1-B concurrent idempotency（per-symbol lock/unique key，HIGH）

## 十、候选方案对比

| 方案 | 解决 | 不能解决 | 风险 | 复杂度 | rollback |
|---|---|---|---|---|---|
| A. precheck before exchange order | local dup；PMB-30（同根） | exchange-only dup；save 失败 retry；concurrent race | low | low | single commit |
| B. idempotency key / client orderId | retry-safe；broker-level dedupe | 需 clientOrderId 全链路；行为面大 | high | high | medium |
| C. exchange-position-aware precheck | exchange-only dup warning | 每 open +1 READ IO（C1 反向）、race 仍在 | medium | medium | medium |

建议最小票：T1-A（方案 A precheck）—— 成功路径不动，只把 dup-check 提前到
load 之后；PMB-30 的 dup-open 分支自然消失。

## 十一、Unresolved Product Decisions（显式）

1. same symbol+side 不同 system 并存（当前隐含 no）
2. same symbol opposite side → A/B/C（当前隐含 REJECT）
3. idempotent-success vs reject return（当前 True；precheck 后推荐维持 True）
4. closed-marker 4h gate 计入 duplicate（当前在 strategy 层作用域外）

## 十二、Return contract（frozen）

- 当前 dup path：return True（策略层已 gate；PM 层 defense-in-depth）
- precheck 后目标 = return True（idempotent success；0 新 side effects）
- 保护侧 target（P9-03B）：被拒时 0 real order / 0 enqueue SL / 0 state write
