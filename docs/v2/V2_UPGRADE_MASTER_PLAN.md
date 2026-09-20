# trade_engine V2 后续升级总路线图

> 基线：`codex/p10-d3d1c` / `f5f78c2`。本文件是实施顺序与发布门禁，
> 不是部署授权。main 在完成发布门禁前继续独立运行。

## 1. 当前检查点

已完成并封板：

- Phase 0-9 架构迁移与行为稳定；
- D5A canonical slot、episode authority、Redis CAS、legacy adoption 和
  reconstructed quarantine 基础设施；
- D3D-1B immutable protection queue identity 与 legacy drop；
- D3D-1C worker V1/V2 authority preflight 和 Redis mutation claim。

当前 V2 **不可部署**：R4A native active-open producer 和 R4A-CLOSE canonical close-to-FLAT 已完成；
migrated、break-even、trailing、legacy lifecycle producers 仍未接线，durable restart
replay 也未实现。

## 2. 固定实施原则

1. main 运行目录、systemd 服务和数据存储不得被开发分支测试触碰。
2. 每张 production ticket 单独分支、单独 commit、可单独 revert。
3. characterization/golden 先于行为修改；旧行为只在票面明确授权时改变。
4. authority、operation journal、desired state 禁止 file fallback。
5. identity 不得混用：request、slot、episode、protection generation、exchange
   order/algo ID、marker token 各自独立。
6. mixed-version mutators 禁止同时运行；最终切换必须有停写窗口。
7. 缺失、冲突、不可读、UNKNOWN 一律 fail closed，不得根据 symbol 猜 owner。

## 3. 总依赖链

```text
D5A + D3D-1B/C（已完成）
  -> R1 canonical live projection + revision
  -> R2 durable desired-protection record/generation
  -> D3D-1D V3 conditional alias writeback
  -> R4 active producer/episode wiring
  -> D2B/C/D durable protection handoff + UNKNOWN recovery
  -> D5D/D3D-3 marker fencing + D3D-4 reconcile CAS
  -> D1 open idempotency
  -> D4 persistence decisions
  -> D3 operation journal / resolver / replay
  -> close/accounting hardening
  -> testnet/canary/cutover
```

Product-policy work may run in parallel, but no implementation crosses an
unapproved product gate.

## 4. Wave R — 先封闭 protection episode fence

### R1 — Canonical Live Position Projection（新前置票）

状态：**IMPLEMENTED / CLOSED（dormant V2 infrastructure）**。实现证据见
`P10_R1_CANONICAL_LIVE_PROJECTION_IMPLEMENTATION.md`；active runtime behavior 未改变。

目标：为当前 operational projection 增加可条件更新的身份和 revision，而不是
继续依赖 symbol-only whole-snapshot save。

最小字段：

```text
exchange_position_key
episode_id
slot_generation
identity_provenance
state_revision
legacy_position_id_alias
```

交付：

- versioned projection schema 和严格 parser；
- per-slot CAS adapter，typed `APPLIED/ALREADY_APPLIED/STALE/UNAVAILABLE/UNKNOWN`；
- legacy row 只允许 controlled adoption 或 quarantine；
- whole-snapshot legacy writer 与 CAS writer 的 mixed-version guard；
- dormant migration/read compatibility tests。

禁止：active open 接线、自动把 `position_id` 升格为 episode、自动激活
reconstructed exposure。

### R2 — P10-D2A Durable Desired Protection

状态：**IMPLEMENTED / CLOSED（dormant V2 infrastructure）**。实现证据见
`P10_R2_DESIRED_PROTECTION_IMPLEMENTATION.md`；active runtime behavior 未改变。

目标：建立同一 episode 内真正权威的 protection generation。

最小记录：

```text
slot + episode_id + slot_generation
protection_generation + desired trigger/qty/side
status + exchange aliases + revision
created_at + updated_at
```

规则：新 desired spec 才递增 generation；相同 intent retry 不递增。Redis
required/no-fallback；exchange `algoId` 只是 alias。

### R3 — P10-D3D-1D V3 Conditional Writeback

状态：**IMPLEMENTED / CLOSED（isolated V2）**。实现证据见
`P10_R3_CONDITIONAL_ALIAS_WRITEBACK_IMPLEMENTATION.md`；R4A 已完成，R4 其余 producer 尚未接线。

前置：R1、R2。

流程：exchange ACK 后，在 mutation claim 仍有效时，对 expected slot、episode、
slot generation、protection generation、projection revision 做原子 CAS。旧 ACK
只记录 stale/diagnostic，不得写入当前 episode。

### R4 — Active Producer And Episode Wiring

状态：**IN_PROGRESS**。R4A native active open 已实现/关闭，证据见
`P10_R4A_NATIVE_OPEN_PRODUCER_IMPLEMENTATION.md`；R4A-CLOSE 也已实现/关闭，证据见
`P10_R4A_CLOSE_FINALIZATION_IMPLEMENTATION.md`；其余 producer 尚未接线。

按顺序接线：
1. native active open（R4A：IMPLEMENTED / CLOSED）；
2. canonical close-to-FLAT finalization（R4A-CLOSE：IMPLEMENTED / CLOSED）；
   - R4B-IDENTITY atomic legacy authority + projection handoff: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-WRITER per-symbol legacy snapshot fence planner: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-MUTATION authority-fenced projection reduction CAS: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-STATE-ACK strict typed snapshot CAS: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-COMPOSE atomic canonical-token + legacy-snapshot commit: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-ACK-POLICY typed non-swallowing caller decision service: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-ACK-PROPAGATION continue-only-after-ack lifecycle boundary: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-RECOVERY-HANDOFF backend-neutral blocked-outcome durable port: IMPLEMENTED / CLOSED (DORMANT);
   - R4B acknowledged migrated-protection pure planner: IMPLEMENTED / CLOSED (DORMANT);
   - R4B exact-ACK migrated desired declaration boundary: IMPLEMENTED / CLOSED (DORMANT);
   - R4B exact-declaration to one-step verification composition: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-PROTECTION-VERIFY strict pure exchange-evidence core: IMPLEMENTED / CLOSED (DORMANT);
   - R4B-PROTECTION-COMMIT verified ACTIVE three-token CAS: IMPLEMENTED / CLOSED (DORMANT);
   - D2C strict Binance observation normalization + bounded reverification: IMPLEMENTED / CLOSED (DORMANT);
   - D2C one-step query/normalize/verified-ACTIVE coordinator: IMPLEMENTED / CLOSED (DORMANT);
   - D2C explicit-endpoint injected signed query transport: IMPLEMENTED / CLOSED (DORMANT);
   - D2C default-off endpoint/scheduler/operator activation gate: IMPLEMENTED / CLOSED (DORMANT);
   - active migration remains gated by caller acknowledgement propagation, lifecycle mutation wiring, and exchange protection verification.
3. controlled migrated position；
4. break-even replacement；
5. trailing replacement；
6. legacy lifecycle path仅在确认仍需支持后接线。

每个 producer 必须从已经 ACK 的 authority/desired record 获取 identity，禁止
enqueue 时临时推导。完成前 legacy 四元任务继续 drop。

## 5. Wave P — Protection 完整可靠性

### D3A — PostgreSQL Operation Journal

PostgreSQL owns durable operation stage/recovery; Redis strict authority owns
current slot/projection/desired CAS; Binance owns physical truth. The dormant
schema/domain foundation and injected create/read/version+owner CAS adapter are
IMPLEMENTED / CLOSED (DORMANT); they have no DSN, driver, or runtime wiring.
DB-clock lease fencing is also IMPLEMENTED / CLOSED (DORMANT).
The schema/CAS/claim race passes an ephemeral PostgreSQL 16 integration test.
Bounded one-per-slot recovery discovery/claim is IMPLEMENTED / CLOSED
(DORMANT) and passes a two-worker PG16 race. The typed stage decision engine
is also IMPLEMENTED / CLOSED (DORMANT), including query-only ambiguity and
ghost/external policy guards. Directive executors, evidence resolvers, extended
fault/concurrency QA, and runtime activation gates remain. A pure D3B batch
boundary now composes claims and decisions while failing closed on journal
UNKNOWN or claim invariant violations; it is not runtime-wired. A pure D3D
generation fence now validates exact operation/authority/desired identity while
keeping OPEN scale-in and external disposition behind explicit policy gates.
Its injected read coordinator is also IMPLEMENTED / CLOSED (DORMANT): typed
authority/desired outcomes remain distinct, concrete Redis adapters are not
imported, and no mutation or runtime wiring is present. The exact canonical
witness/second-read revalidation guard is also dormant and complete. Mutation
ownership and executor composition remain.

A dormant D3B/D3D recovery admission coordinator now re-derives every work-item
decision, composes generation reads, and partitions policy, stale-generation,
dependency-unknown, and invariant failures. Query/local directives may be
marked ready for a future executor; conditional mutation always remains
`MUTATION_OWNERSHIP_REQUIRED`. It has no executor or runtime wiring.

### P1 — P10-D2C Exchange Algo API Characterization

冻结 create timeout、duplicate、query、cancel、terminal status、client identity
能力和 testnet 差异。只做 characterization，不改行为。

### P2 — P10-D2B Durable Handoff

以 durable desired record 替代 process-memory queue 作为恢复依据；实现 claim、
backpressure、worker heartbeat、restart scan 和幂等领取。内存队列可以保留为
wakeup hint，但不能是唯一事实。

### P3 — P10-D2D UNKNOWN Resolver

对 create/cancel timeout 做 bounded query，输出 `CONFIRMED/ABSENT/UNKNOWN`；
generation-fenced reconcile 只恢复原 intent，不向当前 episode重绑旧结果。

### P4 — P10-D2E Safety Policy（产品门）

需批准：fill-to-protection SLO、worker unhealthy 时是否停止新开仓、失败后的
告警/重试/隔离/补偿平仓策略，以及 replace gap/overlap 偏好。

建议初始政策：worker/authority 不健康即暂停新开仓；现有仓只告警并进入
reconcile，不自动市价平仓；所有自动 emergency close 另票审批。

2026-09-20 已批准并实现 dormant 默认契约：风险增加事件过期取消，UNKNOWN
只查询不盲重试，上下文变化后旧事件作废并重建决策，未保护仓位按“恢复保护
→ 暂停新开仓 → quarantine”升级，人工审批绑定版本且过期失效，自动市价平仓
默认关闭。生产时限数值、durable scheduler、告警和 executor wiring 仍未启用。

人工关注面采用 Telegram 通知 + Web Decision Center：TG 只负责提醒和安全
deep link，Web 展示触发快照与当前状态 diff、倒计时、自动兜底、证据时间线和
审计。先交付 durable inbox/outbox 与只读页面，再启用带 RBAC、重新认证、过期
绑定和服务端二次校验的批准动作；任何 UI 不在线都不得阻塞自动安全兜底。
独立的 durable inbox/outbox PostgreSQL schema 与纯状态模型现已作为 dormant
foundation 完成；尚未应用 schema，也没有 PG adapter、Telegram transport、API、
前端、scheduler 或 runtime wiring。

## 6. Wave I — Open Identity 与幂等

1. D1C：Binance client-order-ID/query characterization；
2. D1A：批准 request identity、duplicate scope、scale-in、side flip、跨系统所有权；
3. D1B：durable reservation + lease/fencing + retention；
4. D1D：UNKNOWN ACK 查询、恢复、人工 override；
5. active open 接线与 shadow comparison。

建议 V2 首发政策：一个 physical slot 同时只允许一个 ACTIVE episode；禁止自动
scale-in 和跨系统共持，后续再以独立产品票开放。

## 7. Wave M — Marker、Monitor 与 Reconcile

### M1 — D5D / PMB-4A/B/C

拆分 close lease、episode tombstone、accounting dedup、cooldown marker；全部携带
episode/generation/token，并使用 compare-and-clear。TTL 只负责 retention，不
承担身份正确性。

### M2 — D3D-4 Projection Fence

monitor、ghost cleanup、external reconcile 的每次写入都比较 expected episode、
slot generation 和 revision。exchange snapshot 可证明 physical exposure，不能
恢复 native lineage；未知 exposure 进入 reconstructed quarantine。

### M3 — PMB-24 产品门

明确 divergence 是 internal repair、business close 还是 quarantine。未批准前
禁止 reconcile 自动执行不可逆业务平仓。

## 8. Wave J — Persistence、Journal 与 Crash Recovery

### J1 — D4 产品政策

批准 sink 角色：

- Redis authority/operation/desired state：required、acknowledged、no fallback；
- file：best-effort recovery copy，不是 authority；
- PostgreSQL event/close accounting：明确 required 或 best-effort；
- ClickHouse：historical advisory；
- Telegram：best-effort 或 bounded retry，不能冒充业务 ACK。

### J2 — D3A Operation Journal

实现 operation schema、legal transitions、CAS、lease、retention、UNKNOWN 和
operator annotations。先 dormant，不接 active exchange mutation。

### J3 — D3C Exchange Evidence Resolver

统一 order/fill/position/algo evidence 查询，严格区分 NOT_FOUND、UNAVAILABLE、
AMBIGUOUS 和 CONFIRMED。

### J4 — D3B Replay Coordinator

startup + continuous scan；按 stage 幂等恢复，先 reconcile 后 retry。任何 stale
episode/generation journal 必须终止，不得转绑 current owner。

### J5 — D3E Compatibility Results

内部统一 typed result；仅在 legacy facade 边界映射 bool/None，避免 UNKNOWN 被
压成失败或成功。

## 9. Wave C — Close、Partial Close 与 Accounting

依赖 R、M、J 和 D4 政策：

- full/partial close operation journal；
- reduce-only ACK/UNKNOWN resolver；
- close result episode/revision CAS；
- authoritative accounting sink 和 finalization ACK；
- ghost close replay、duplicate/loss policy；
- notification 与业务 completion 解耦。

Emergency full reduce-only close 始终保留 authority-independent break-glass
入口，但必须记录独立 operation/evidence。

## 10. 可并行工作流

在不接 active mutation 的前提下可以并行：

- Binance order/algo characterization（D1C、D2C）；
- R1/R2 dormant schema、parser、CAS 和 concurrency tests；
- observability vocabulary、metrics 和 runbook；
- product decision records；
- replay fixture、fault-injection harness、testnet evidence collector。

不得并行合并的链：R1 → R2 → R3 → R4，以及 J2/J3 → J4。

## 11. 每张票统一验收模板

每张 production ticket 至少满足：

```text
1. ticket-specific characterization/golden
2. concurrency/ABA/backend-unavailable/malformed tests
3. tests/phase10
4. tests/position_manager
5. tests/execution
6. full pytest
7. Ruff（新增文件必须全绿；历史文件不得新增告警）
8. git diff --check
9. scope/rollback/known-gap 文档
10. 单独 commit，clean worktree
```

涉及 Redis Lua 时增加真实临时 Redis 集成测试；涉及 Binance 时只允许 mock 或
明确隔离的 testnet，默认测试不得触发真实订单。

## 12. 发布阶段与门禁

### G0 — Offline complete

全套测试、migration dry-run、rollback rehearsal、配置校验、无 secret 入库。

### G1 — Testnet

独立 principal/environment/Redis namespace；验证 open/protection/replace/close、
timeout、restart、Redis loss、duplicate worker 和 ABA。禁止复用 production key。

### G2 — Production shadow read

V2 只读取 exchange/authority并计算 decision，不下单、不 cancel、不写 authority；
与 main 输出做差异报告。

### G3 — Authority shadow write

只在独立 namespace 写 candidate authority/journal，与 main 无交互；验证 adoption
分类和 migration dry-run。

### G4 — Canary mutator

停掉同一 slot 的 main mutator 后，仅选择允许的 account/symbol canary。必须具备
kill switch、open pause、claim/journal dashboard 和人工回退 runbook。

### G5 — Full cutover

1. 全局暂停新开仓；
2. 停止 main mutators，确认进程退出；
3. 备份 Redis/配置/版本，记录 exchange snapshot；
4. 清空仅内存 legacy queue（通过进程退出，不迁移四元任务）；
5. dry-run 后执行 controlled adoption/quarantine；
6. 启动 V2 单实例，先 reconcile 再开放 worker；
7. 验证 authority、protection、journal、告警后逐步开放新开仓；
8. 观察窗口通过后再扩大实例/标的。

禁止在 main 仍运行时直接覆盖文件或启动 V2 mutator。

## 13. 回滚原则

- code rollback 不回退 generation/fencing high-water；
- 一旦 V2 写入新 schema，旧 main 未证明兼容前不得直接启动；
- exchange effect UNKNOWN 时先 reconcile，禁止 blind retry；
- canary rollback 先 pause open/worker，再停 V2，保存 journal/evidence，最后按
  runbook 启动兼容版本；
- migration/adoption 必须有 manifest，不能靠删除 Redis key“恢复”。

## 14. 产品决策清单

进入相应 wave 前必须签字确认：

1. scale-in、side flip、跨系统 slot ownership；
2. protection SLO、失败动作、replacement gap/overlap；
3. Redis/PG/CH/file/notification 的 required/best-effort 角色；
4. close accounting authority 与 duplicate-versus-loss 偏好；
5. reconcile divergence policy；
6. marker retention/cooldown 跨 episode 语义；
7. UNKNOWN retention、自动恢复次数和人工 override；
8. legacy adoption maintenance window 与 reconstructed exposure 审批方式。

## 15. 下一步执行顺序

立即执行：

```text
R1 canonical live projection/revision contract
-> R2 durable desired protection record
-> R3 D3D-1D V3 writeback CAS
-> R4 active producer wiring
```

同时启动不改 production behavior 的 D1C/D2C API characterization 和上述产品
decision records。完成 R4 前，当前 D3D1B/C 分支始终保持“不可部署”。
