# trade_engine V2 架构迁移 — Codex 接手文档

> 用途：让 Codex 在不重放 ChatGPT / OpenCode 历史对话的前提下，直接接手 `trade_engine` 的后续 V2 架构改造。
>
> 仓库：`Anson-blip-xxx/trade_engine`  
> 工作分支：`feature/v2-architecture`
>
> **封板节点：P10-07D / D5A-3 Episode Authority Onboarding — PASS / CLOSED**  
> **最新已确认 commit：`e7f1e64 feat(v2): add episode authority onboarding`**  
> **下一张票：P10-D3D-1B — Immutable Queue Episode Identity Extension**
>
> 重要：Codex 接手后必须先核对实际 `HEAD`、工作树和测试结果。本文记录的是 2026-09-18 封板状态；若仓库现实与本文冲突，以仓库现实为准，但不得无理由改写既有 Golden / Characterization 契约。

---

# 1. 总原则

这个项目已经完成大规模架构梳理，后续不要重新设计一遍。

最重要的原则：

> **Behavior Preservation > Architecture Purity / Refactor Beauty**

长期执行规则：

1. 先 Characterization / Golden，再改 production；
2. 架构清理与行为修复必须拆票；
3. Frozen behavior 不允许“顺手修”；
4. 每次 production behavior change 必须最小化、可回滚；
5. 每张修复票尽量一个 fix commit；
6. Characterization commit 不因 fix rollback 而撤销；
7. 如果旧 Golden 失败，默认先视为行为被破坏，而不是直接改 expected；
8. S7 继续 `KEEP / DEFER`，除非单独开启迁移；
9. Phase 10 已进入 reliability / semantic hardening，不允许回到“大重构模式”。

---

# 2. 当前总体架构

核心链路：

```text
S3 / TradingView event
        ↓
Unified Signal
        ↓
S6 / S8
        ↓
Decision / Risk
        ↓
strategies.shared_executor.open_position
        ↓
ExecutionService
        ↓
Binance adapter / exchange
        ↓
Position lifecycle / state / protection / reconcile
```

Phase 10 当前已经额外建立了一套 dormant authority 基础设施：

```text
position_identity/
    principal.py
    slot.py
    authority.py
    authority_redis.py
    adoption.py
```

当前这些 identity / authority 组件**尚未接入 active open / close / protection / reconcile 主链**，这是刻意保持的边界。

---

# 3. 固定 Roadmap 状态

```text
Phase 0   Audit                                  ✅ CLOSED
Phase 1   Journal / Replay / PM Golden            ✅ CLOSED
Phase 2   Unified Signal                          ✅ CLOSED
Phase 3   Decision / Risk                         ✅ CLOSED
Phase 4   Execution Architecture                  ✅ CLOSED
Phase 5   S3 Event Architecture                   ✅ CLOSED
Phase 6   S0 Regime Engine                        ✅ CLOSED
Phase 7   PositionManager Decomposition            ✅ CLOSED
Phase 8   Architecture Cleanup                    ✅ CLOSED
Phase 9   Behavior Stabilization                  ✅ CLOSED
Phase 10  Reliability & Semantic Hardening        ← CURRENT
```

Phase 9 closure commit：

```text
fa6133e docs(v2): close phase 9 behavior stabilization
```

Phase 10 当前已推进至 D5A foundation 完成，下一步进入 protection generation fencing 的 active integration。

---

# 4. Phase 9 最终行为状态

## 4.1 FIXED / CLOSED

- S0-1
- PMB-9
- PMB-17
- PMB-23B
- PMB-27
- PMB-30 duplicate-ordering branch
- T1-A

关键 fix commits：

```text
S0-1      8ee0ea5
PMB-9     f1d3cd2
T1-A      e4a4a2a
PMB-27    eb6c14f
PMB-23B   11d5283
PMB-17    32412d9
```

## 4.2 INTENTIONAL / KEEP CLOSED

- PMB-26A closed-marker self-heal
- PMB-26B Telegram best-effort swallow

## 4.3 Deferred into Phase 10

Product decision：

- T1-B
- PMB-23A
- PMB-24
- PMB-26B2
- PMB-26C1
- PMB-26C2

Design / reliability：

- T5
- T12
- POS-ID
- PMB-4

这些不要按 Phase 9 旧描述直接修；Phase 10 已重新建模。

---

# 5. Phase 10 已完成设计票

## P10-00 Reliability Audit

Commit：

```text
5c927d3 docs(v2): audit phase 10 reliability boundaries
```

核心结论：

- Binance / exchange 是 physical exposure truth；
- Redis/file 是 operational metadata / projection；
- PG/CH 不是当前 lifecycle recovery truth；
- open / close / reconcile 都存在 partial-commit；
- 需要先定义 identity、protection、persistence、crash consistency，再实现可靠性修复。

---

## P10-01 / D1 Open Idempotency Model

Commit：

```text
acb5a53 docs(v2): define open idempotency model
```

核心结论：

- `request idempotency != position exclusivity`；
- `LOCK != RETRY IDEMPOTENCY`；
- timeout / ambiguous ACK 必须进入 `UNKNOWN`，不能 blind retry；
- current `position_id` 不是 request identity；
- 当前 single active symbol position 只是现状，不等于 scale-in 语义已经被产品确认。

---

## P10-02 / D2 Protection Establishment Model

Commit：

```text
2cf2cac docs(v2): define protection establishment model
```

核心结论：

- T5 不应描述成“固定 11 秒 gap”；
- 真正风险是 **unbounded fill-to-protection gap**；
- protection 需要 desired state、generation、UNKNOWN、restart-safe handoff；
- protection failure / SLO 仍有产品风险策略依赖。

---

## P10-03 / D5 Position Identity & Marker Authority

Commit：

```text
5039977 docs(v2): define position identity and marker authority
```

核心结论：

- `request_id != position_episode_id`；
- confirmed flat 后 reopen 必须是新 episode；
- `symbol` 当前承担了过多 identity 责任；
- marker TTL 只能解决 retention，不能解决 episode / ABA / generation；
- protection 应绑定：

```text
(exchange_position_key, position_episode_id, protection_generation)
```

---

## P10-04 / D4 Persistence Failure Policy

Commit：

```text
fca3fec docs(v2): define persistence failure policy
```

核心结论：

- sink 被写入不等于 source of truth；
- `pm:positions` 候选为 `REQUIRED + RETRYABLE` operational projection；
- file fallback 只是 best-effort secondary recovery copy；
- PG event ledger 候选 best-effort，PG close/accounting authority另议；
- CH 是 historical / derived advisory，不是 lifecycle recovery source；
- 不追求 Binance + Redis + PG 的“全局 exactly-once”。

---

## P10-05 / D3 Crash Consistency Model

Commit：

```text
9c86030 docs(v2): define crash consistency model
```

Phase 10 统一模型形成：

```text
durable intent
+ exchange ambiguity reconciliation
+ idempotent local effects
+ episode/generation fencing
+ restart replay
```

目标不是“永不崩溃”，而是崩溃后能证明：

- 发生了什么；
- 没发生什么；
- 下一步什么动作安全。

---

# 6. Generation Fencing 审计与 authority 收敛

## P10-06A / D3D Generation Fencing Audit

Commit：

```text
a93c689 test(v2): audit stale async work fencing
```

已经 deterministic reproduction 的真实 ABA：

```text
Episode A protection task queued
A closes
Episode B reopens same symbol
old A task executes
→ may cancel B protection
→ may create A stop against B
→ may write new algoId into B
```

当前 `_ALGO_QUEUE` 精确旧 schema：

```text
(symbol, side, trigger_price, qty)
```

它没有：

- episode identity
- slot generation
- protection generation

旧四元组后续正式策略：

```text
LEGACY_UNFENCED -> DROP
```

---

## P10-06B / D3D-1 Episode Fence Authority Contract

Commit：

```text
f8b26bc docs(v2): define episode fence authority contract
```

Canonical model：

```text
exchange_position_key
+ episode_id
+ slot_generation
+ protection_generation
```

重要裁决：

```text
current position_id = TEMPORARY_FENCE_ONLY
```

不能把当前 `position_id` 固化成 canonical episode token。

Worker 最终应做三次验证：

```text
V1 before cancel
V2 before create
V3 before writeback
```

任何 missing / malformed / mismatch / legacy identity：

```text
NO CANCEL
NO CREATE
NO WRITEBACK
```

---

# 7. D5A Episode Authority Foundation — 已完成

## P10-07 Readiness

Commit：

```text
24af989 docs(v2): finalize episode authority readiness
```

确定：

- account principal = explicit, non-secret, rotation-stable logical alias；
- environment：`PROD | DEMO | SANDBOX`；
- 当前 mode：`ONE_WAY / BOTH`；
- dedicated Redis slot authority；
- Lua CAS；
- slot generation 是 durable high-water；
- legacy = controlled adoption or quarantine；
- reconstructed = `RECONSTRUCTED_QUARANTINED`；
- emergency full close 不依赖 episode authority。

---

## P10-07B / D5A-1 Slot Namespace + Principal Resolver

Commit：

```text
d2318cd feat(v2): add canonical exchange slot identity
```

新增 production leaf：

```text
position_identity/__init__.py
position_identity/principal.py
position_identity/slot.py
```

没有修改业务 production 文件。

Canonical physical slot fields：

```text
exchange
product
environment
account_principal_id
position_mode
symbol
slot_side
```

当前：

```text
BINANCE
FUTURES
PROD | DEMO | SANDBOX
explicit logical principal
ONE_WAY
BOTH
```

序列化：sorted compact canonical JSON + SHA-256 Redis-safe storage key。

关键：

- API key / secret / private key / wallet 不进入 principal；
- API credential rotation 不改变 slot identity；
- S6/S8 system 不进入 physical slot identity；
- legacy `position_id` 不进入 slot identity；
- request_id 不进入 slot identity。

---

## P10-07C / D5A-2 Durable Slot Authority Store

Commit：

```text
f586f26 feat(v2): add durable slot authority store
```

新增：

- immutable slot authority domain；
- dedicated Redis authority adapter；
- Redis Lua CAS；
- typed acknowledgements；
- generation high-water；
- revision/version；
- malformed / backend / ABA / concurrency tests。

曾修复真实 Redis `cjson.null` initial FLAT allocation 兼容问题。

关键 invariant：

```text
slot_generation != revision
```

- generation：per-slot episode high-water，永不回退/复用；
- revision：authority record mutation CAS version；
- FLAT 后 generation 仍保留；
- authority key 无 TTL；
- 无 file fallback；
- 不耦合 `pm:positions`；
- 当前仍无 active runtime caller。

---

## P10-07D / D5A-3 Episode Authority Onboarding

封板 commit：

```text
e7f1e64 feat(v2): add episode authority onboarding
```

新增：

```text
position_identity/adoption.py
```

并仅更新 `position_identity/__init__.py` exports。

D5A foundation 到此：

```text
COMPLETE
```

### Legacy classification

Typed classes 包括：

```text
LEGACY_ADOPTABLE
LEGACY_QUARANTINE
NATIVE_AUTHORIZED
CONFLICT
INVALID
```

Controlled adoption：

- 需要明确 principal / slot；
- exchange/local evidence 一致；
- authority CAS 无冲突；
- provenance=`MIGRATED`；
- legacy position id 只保留 alias；
- 并发 adopter 只有一个 winner；
- loser reload canonical winner；
- adoption 前 legacy exposure 不允许 future async protection mutation。

### Reconstructed exposure

Exchange-only、lineage 无法可信恢复：

```text
RECONSTRUCTED_QUARANTINE
```

Apply 后：

```text
provenance = RECONSTRUCTED
status = QUARANTINED
```

不得自动 ACTIVE，不得自动 protection mutation。

### ACTIVE mutation predicate

未来 async mutation 至少要求：

```text
status == ACTIVE
episode_id present
slot_generation > 0
provenance present
```

### Emergency close

仍然 authority-independent。

不要让 episode migration / quarantine 阻断 break-glass full reduce-only close。

---

# 8. 当前测试基线（封板）

P10-07D 完成后的真实基线：

```text
position_identity complete suite: 92 passed
Phase 10:                         138 passed
PositionManager:                  765 passed, 1 existing warning
Execution:                        421 passed
Full suite:                       2301 passed, 1 skipped, 1 existing warning
Ruff:                             PASS
git diff --check:                 PASS
Working tree:                     clean
```

Codex 接手时必须先重新跑并确认。

---

# 9. 当前 production behavior 重要事实

## 9.1 Active open path

当前确认：

```text
S3 / TV event
→ S6 / S8
→ strategies.shared_executor.open_position
→ ExecutionService
→ Binance
```

Legacy `PositionLifecycleService.open_position` 没有 repository production caller。

## 9.2 Current open duplicate protection

T1-A 已修：local duplicate precheck 前移到 exchange mutation 之前。

但是：

> precheck != concurrency-safe idempotency

T1-B 仍不是“加一个锁”就能完成。

## 9.3 Current protection worker

历史事实：

- `_ALGO_QUEUE` 是 process-memory list；
- FIFO `pop(0)`；
- restart 会丢；
- worker task 后 sleep 11s；
- idle sleep 1s；
- 当前旧 queue payload 为 4-tuple；
- 没有 episode / protection generation fencing。

不要把 T5 再写成“固定 11 秒 initial delay”。

## 9.4 Current marker

历史 model：

```text
closed:{symbol} -> {"ts": float}
```

约 4h lazy timestamp compare，无 Redis TTL。

Phase 10 已确认：

- TTL 不是 identity 修复；
- symbol-only clear 有 ABA 风险；
- marker 后续应绑定 episode / generation / operation token；
- 但不是当前下一票。

---

# 10. 下一张票：D3D-1B Queue Identity Extension

这是 Codex 接手后的第一张建议任务。

目标：

> 把 immutable episode identity 带进 protection queue，但**还不让 worker enforce fence**。

建议保持小步：

```text
D3D-1B Queue Identity Extension
    ↓
D3D-1C Worker Preflight Fence
    ↓
D3D-1D Conditional Writeback
```

不要把三张合成一个大 commit。

---

# 11. D3D-1B 建议实现边界

## 11.1 Future queue payload

目标 payload 至少携带：

```text
exchange_position_key
episode_id
slot_generation
protection_generation
```

结合旧字段，候选概念：

```text
(
    symbol,
    side,
    trigger_price,
    qty,
    exchange_position_key,
    episode_id,
    slot_generation,
    protection_generation,
)
```

推荐优先考虑 immutable dataclass/value object，避免 tuple index 持续膨胀。

但：

> 不要为了“更优雅”顺手重构 worker 其他逻辑。

## 11.2 Legacy 4-tuple

正式规则已经确定：

```text
LEGACY_UNFENCED -> DROP
```

禁止：

- 根据当前 symbol 自动补成 current episode；
- 根据 current `position_id` 猜 episode；
- 把 legacy task 升级成当前 task。

## 11.3 D3D-1B 本票不要做

不要提前实现：

- cancel 前 V1 fence；
- create 前 V2 fence；
- writeback 前 V3 fence；
- CAS writeback；
- restart replay；
- operation journal；
- marker generation fencing；
- reconcile revision fencing。

这些属于后续票。

---

# 12. D3D-1C / D3D-1D 已定原则

后续 worker enforce 时必须做到：

### V1 — before cancel

确认当前 authority：

```text
slot matches
episode_id matches
slot_generation matches
protection_generation matches
status == ACTIVE
```

不匹配：

```text
STALE -> DROP
```

### V2 — before create

因为 cancel 和 create 之间可能发生变化，必须重新验证。

### V3 — before writeback

`algo_sl_id` 写回不能只是：

```python
positions[symbol]['algo_sl_id'] = ...
```

必须条件写入 expected：

```text
episode_id
slot_generation
protection_generation
revision
```

否则老 worker 仍可污染新 episode。

---

# 13. Fail-closed fencing 原则

针对 async protection mutation：

任一情况：

```text
identity missing
identity malformed
authority unreadable
episode mismatch
slot generation mismatch
protection generation mismatch
legacy unfenced item
```

结果必须：

```text
NO CANCEL
NO CREATE
NO WRITEBACK
```

注意：

这会产生“当前 episode 可能暂时无保护”的风险。

该风险属于 D2 protection emergency / admission policy；**不能为了避免无保护就 blind execute 一个身份不明的旧 task**。

---

# 14. 不要重新打开的历史 Frozen Behavior

除非对应 ticket 明确允许，否则不要修改：

- S7 行为；
- S0 fail-open 语义；
- S3 已冻结 event semantics；
- dual PnL；
- negative partial-close historical behavior；
- PMB-29 cooldown noop；
- PMB-26A self-heal；
- Telegram current best-effort swallow；
- PMB-26C persistence behavior（未完成产品政策前）；
- PMB-24 destructive internal synchronization semantics；
- marker identity / TTL（等后续 D3D-3 / PMB-4 tickets）；
- open idempotency full implementation（D1/T1-B 尚未接 active foundation）。

---

# 15. Codex 接手后的第一组命令

先不要写代码。

```bash
git status --short
git branch --show-current
git log --oneline -30

git show --stat --oneline e7f1e64

git diff --check

pytest -q tests/phase10
pytest -q tests/position_manager
pytest -q tests/execution
pytest -q

ruff check position_identity tests/phase10 tests/position_manager
```

然后检查：

```bash
find position_identity -maxdepth 2 -type f | sort
find docs/v2 -maxdepth 1 -type f | sort
find tests/phase10 -maxdepth 2 -type f | sort
```

必须确认：

```text
branch = feature/v2-architecture
working tree = clean
HEAD 至少包含 e7f1e64
```

如果 HEAD 已超前，先阅读后续 commit，不要强行 reset 到本文节点。

---

# 16. Codex 优先阅读文档

Phase 10 接手优先级：

```text
docs/v2/PHASE10_BACKLOG.md

docs/v2/P10_D1_OPEN_IDEMPOTENCY_MODEL.md
docs/v2/P10_D2_PROTECTION_ESTABLISHMENT_MODEL.md
docs/v2/P10_D3_CRASH_CONSISTENCY_MODEL.md
docs/v2/P10_D4_PERSISTENCE_FAILURE_POLICY.md
docs/v2/P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md

docs/v2/P10_D3D_GENERATION_FENCING_AUDIT.md
docs/v2/P10_D3D1_EPISODE_FENCE_AUTHORITY.md
docs/v2/P10_D5A_EPISODE_AUTHORITY_READINESS.md

docs/v2/P10_D5A1_SLOT_NAMESPACE_IMPLEMENTATION.md
docs/v2/P10_D5A2_SLOT_AUTHORITY_IMPLEMENTATION.md
docs/v2/P10_D5A3_EPISODE_ONBOARDING_IMPLEMENTATION.md
```

然后看对应 tests：

```text
tests/phase10/
tests/position_manager/
```

不要只读生产代码猜语义。

---

# 17. position_identity 当前契约

Codex 不应重新发明这些概念。

## Physical slot

```text
exchange
+ product
+ environment
+ account_principal_id
+ position_mode
+ symbol
+ slot_side
```

## Episode authority

```text
exchange_position_key
+ episode_id
+ slot_generation
+ status
+ provenance
+ revision
```

## Provenance

```text
NATIVE
MIGRATED
RECONSTRUCTED
```

## Reconstructed

默认：

```text
RECONSTRUCTED_QUARANTINED
```

绝对不要自动 ACTIVE。

## Generation

```text
slot_generation
```

- durable per-slot high-water；
- flat 后保留；
- next episode = N+1；
- 永不复用。

```text
protection_generation
```

- same episode 内 desired protection version；
- initial / break-even / trailing 应分别推进；
- 不等于 slot generation。

---

# 18. Identity 不允许混用

以下 identity 必须继续分离：

```text
signal/event identity
request identity
exchange order identity
fill identity
exchange physical slot identity
position episode identity
strategy ownership
protection intent identity
protection generation
marker/tombstone identity
```

尤其不要做：

```text
request_id == episode_id
position_id == canonical episode_id
symbol == episode identity
system == physical slot identity
algo_id == protection identity
```

---

# 19. Redis / Persistence 约束

当前 authority store：

- dedicated slot key；
- 不复用 `pm:positions`；
- no TTL；
- no file fallback；
- Lua CAS；
- typed ack；
- malformed state 不得 blind overwrite。

不要把 `pm:positions` snapshot save 当作 slot authority CAS。

也不要通过 Redisson lock 替代 generation correctness。

原则：
> lock 可以防 overlap，但不能证明 lineage / generation / retry identity。

---

# 20. Test / Commit 规则

每张 production ticket 必须至少：

```bash
pytest -q <ticket-specific tests>
pytest -q tests/phase10
pytest -q tests/position_manager
pytest -q tests/execution
pytest -q
ruff check <changed python paths>
git diff --check
```

提交前：

```bash
git status --short
git diff --stat
git diff
```

确认：

- ticket 边界内修改；
- 没有顺手修 frozen behavior；
- 没有改 S7；
- 没有意外修改 Redis key/value/TTL；
- 没有改变 exception swallow/propagate；
- 没有改变 call order；
- Golden expected 只在 ticket 明确改变行为时更新。

---

# 21. Commit / Rollback 规则

继续小 commit。

典型风格：

```text
docs(v2): ...
test(v2): ...
feat(v2): ...
refactor(v2): ...
fix(v2): ...
```

行为修复：

> rollback 应指向 FIX commit，不要 revert Characterization commit。

如果一张票同时包含 characterization + fix，优先拆成两个 commits。

---

# 22. 推荐的后续顺序

当前封板后的建议顺序：

```text
P10-D3D-1B
Immutable Queue Episode Identity Extension
        ↓
P10-D3D-1C
Worker Preflight Fence
        ↓
P10-D3D-1D
Conditional Protection Writeback
        ↓
再评估 D3D-1E / D3D-2
Legacy guard / protection generation integration
```

之后再根据 backlog readiness 进入：

- marker compare-and-clear fencing；
- reconcile version fencing；
- operation journal / restart recovery；
- UNKNOWN resolver；
- open idempotency active implementation；
- persistence-policy-dependent tickets。

不要跳过 dependency 顺序。

---

# 23. 当前仍未解决 / 不应误判为已解决

D5A foundation 完成，不代表以下已解决：

- active open request idempotency；
- restart-safe operation journal；
- restart protection replay；
- protection SLO / emergency policy；
- marker generation fencing；
- reconcile CAS/version fencing；
- PG event/ledger 最终产品 policy；
- Telegram delivery retry policy；
- legacy/reconstructed authority 自动扫描/接线；
- active `pm:positions` episode schema integration。

Codex 不要看到基础设施存在就默认主链已经在使用。

---

# 24. Codex 首票工作指令

Codex 接手后第一张票建议直接执行：

```text
P10-D3D-1B — Immutable Queue Episode Identity Extension
```

目标：

1. 先盘点所有 `_ALGO_QUEUE` producer / consumer；
2. 引入 immutable queue task type；
3. payload 携带 canonical slot / episode / slot generation / protection generation；
4. legacy 4-tuple fail-closed drop；
5. 仍不启用 worker authority validation；
6. 行为改变仅限旧 unfenced task 的明确 drop contract；
7. 不做 cancel/create/writeback CAS；
8. characterization / architecture guards 先行；
9. full suite 必须保持绿色；
10. 单独 commit，便于 rollback。

开始前先确认 `PHASE10_BACKLOG.md` 没有比本文更新的 ticket 状态。

---

# 25. 最终接手原则

Codex 后续不要追求“一次把可靠性全部补齐”。

正确方式仍然是：

```text
characterize
→ define authority
→ add dormant primitive
→ wire one boundary
→ fence one mutation
→ verify parity / intended delta
→ commit
→ next ticket
```

当前我们已经完成：

```text
identity foundation
+ slot namespace
+ durable slot authority
+ generation CAS
+ legacy/reconstructed onboarding primitives
```

下一步不是重新设计，而是把这些已经证明过的 authority contract **一层一层接入 protection async path**。

最后牢记：

> 一个 worker 只有在能证明“这个 task 属于当前 slot 的当前 episode、当前 protection generation”时，才有资格执行 cancel / create / writeback。

这就是后续 D3D-1B / 1C / 1D 的核心安全边界。