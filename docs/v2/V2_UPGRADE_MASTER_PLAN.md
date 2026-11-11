# trade_engine V2 后续升级总路线图

## 2026-10-31 维护与结构优化路线（当前）

本节取代下方早期升级票据作为**当前代码维护顺序**，不改变交易策略或授权实盘。
早期的“不可部署”“main 继续独立运行”等状态只描述当时阶段，不能当成当前状态。
代码与运行证据见 [本次清单](CODEBASE_REVIEW_20261031.json)。

### 评估结论

问题不是简单的文件数量，而是四类边界混杂：现行与历史文档、领域与 I/O 适配、
测试与测试公共库、工作树与逐服务发布版本。先固定这些边界，再清理旧代码。

本次源码统计基线（清理前）：`services` 147 个 Python 文件 / 38,969 行，
`v2_core` 123 个 / 31,815 行。`tests/v2_core` 中存在 634 处跨测试模块导入，
其中一个意图测试模块被导入 177 次。`TradingPipeline.run_once` 长达 592 行。
这说明共享测试设施、装配和调度比目录改名更值得优先整理。

2026-10-31 架构测试基线**不是全绿**：`test_v2_core_boundary.py` 实测 1 失败、6 通过。
`jev_advisory` 用 `pathlib` 定位隔离执行脚本，并非本地文件存储业务状态；但 SDK、
子进程传输与领域协议确实混在核心层。另有 `entry_controls` 反向导入
`services.v2_entry_structure.prior_close`。必须修复职责边界，不能扩大忽略名单过关。

2026-11-01 M1 已移除这两处逆向/进程依赖，原架构测试不放宽禁用清单，新增
JEV 纯协议约束后一共 8 项通过。最终数据库回归结果单列在本阶段验收记录中。

### 实施顺序与交付门槛

| 阶段 | 工作 | 门槛与回滚 | 状态 |
|---|---|---|---|
| M0 基线与低风险退役 | 核对真实运行入口、依赖与旧调度；纠正文档；删除两个无引用危险旧脚本 | 只改 V2 工作树；不改交易参数、不重启服务；可从 Git 恢复 | 已完成，172 项相关隔离回归通过，现有架构失败另列 |
| M1 测试与边界修复 | 把高扇入 DB fixture、builder、venue fake 迁到 `tests/support`；规范独立 V2 测试入口；拆 JEV 传输适配；移除 core → services 反向依赖 | 保留网络封锁并加固隔离 DSN 检查；恢复现有架构测试；覆盖 JEV 降级、最终提交锁内复核，不改策略判断 | 已完成；相关隔离回归 1,309 通过；未发布 |
| M2 发布协议与窄依赖 | 服务级 release/hash/配置引用/数据库兼容清单；用不可变 ReleaseSpec 替代日期脚本修改模块全局变量；抽离研究通知的窄适配 | 候选/当前/回滚目标可追溯；离线预检不能执行切换；验证实际发布包而不只测试工作树；保留旧入口兼容 | M1 后 |
| M3 交易编排拆分 | 分拆 `run_once`、`create_testnet_process`、账户上下文构建；装配、维护、退出保护、开仓各有明确接口 | 同输入输出、调用顺序、SQL/锁/超时预算、故障降级轨迹一致；不得借重构改变资金、杠杆或止盈止损规则；灰度组合验收 | M1/M2 后 |
| M4 历史代码与测试隔离 | 按完整依赖簇迁移 legacy 和历史测试；为仍复用的算法保留兼容导出；达到退役门槛后逐簇删除 | 不破坏裸 import/sys.path/硬路径测试；保留“无文件兜底”安全契约；不能删除 `s0.core`、`s3.core`、`s3.detector`、`decision.core` | 分批进行 |
| M5 发布目录生命周期 | 将基础设施稳定配置从旧 release 解耦，再按当前引用与回滚保留集合清理 | dry-run 清单、引用校验、可恢复归档；不按日期或磁盘占用盲删；不删除数据库迁移和验收证据 | 与 M2 配合，单独验收 |

### M1 边界与兼容说明（2026-11-01）

验收记录：[M1_BOUNDARIES_ACCEPTANCE_20261101.json](M1_BOUNDARIES_ACCEPTANCE_20261101.json)。
完整 V2 目录离线运行：6,488 通过、2,261 跳过；本轮相关 PG/Redis 隔离回归：
1,309 通过、0 跳过、0 失败。两组有重叠，不能相加，也不是全部数据库测试都通过的声明。
临时 PG 完成了备份恢复和崩溃恢复测试，退出后清理；交易服务 PID 和启动时间未改变。

- `v2_core/entry_history.py` 保存原 `prior_close` 查询；服务层保留兼容导出，最终
  提交复核只依赖核心。查询 SQL、参数、锁/超时与拒绝判断未改变。
- `services/v2_jev_transport.py` 承担子进程、固定 SDK 和共享解释器适配；
  `v2_core/jev_advisory.py` 保留协议、提示词、预算、响应验证与降级规则。
  新版发布包必须同时包含这两个层次，以及 `entry_history.py`，不能只替换旧单文件。
  `-I` 子进程通过相邻路径加载纯协议，不向子进程注入仓库 `sys.path`。
- 原意图测试模块的 23 个辅助函数迁入 `tests/support/v2_admission.py`，
  150 个测试文件的 177 处导入改用公共库。148 个原测试函数 AST 全部保持不变；
  22 个辅助函数完全等价，数据库 fixture 另行强化隔离校验，不是交易逻辑变更。
- 跨测试模块导入从 634 处降至 457 处；本轮只拆最高扇入模块，剩余分组纳入后续
  M4，不宣称所有测试已彻底解耦。新增公共包不在导入时连接数据库或加载凭据。
- QA 入口只收集 V2、保留网络审计，隔离实例仅走临时 Unix socket。
  真实共享 JEV SDK、交易所请求、业务数据库、systemd 重启及部署不在本轮验证范围。

### M2 最小切入点（已完成静态定位，尚未实施）

2026-11-01 用户报告链路异常后，先处理运行事故，M2 不与热修复混合发布。
事故记录见 [SIGNAL_HISTORY_HOTFIX_20261101.json](SIGNAL_HISTORY_HOTFIX_20261101.json)。
冷历史维护在约 250 万信号上先做全量反连接、最后才 LIMIT，造成反复超时回滚；
热修复改为先有界索引分页、成功提交后推进、循环补漏，并补执行周期异常的脱敏日志。
同一页因补写量大超时时应缩小批次，不能跳过数据或放宽安全条件。实际发布包与工作树
分别验收，未发布的 M1 和其他策略改动不搭车；运行是否恢复以快照和真实周期为准。
健康检查的队列聚合仍有全历史查询，列为紧邻后续优化项：隔离诊断事务、明确未知状态，
历史完整扫描之前不得清除旧积压告警，不能用裁短时间窗口伪装恢复。

本次最终热修复已于 2026-11-01 09:54 UTC+8 前发布至
`/opt/trade-engine-v2/releases/signal-history-controller-20261101-v2`。
实际候选包 114 项针对性隔离回归通过；09:54–09:58:50 期间 19 个交易周期全部完成，
21 次健康状态均无信号延迟/活动告警，保护检查均 CLEAR，路由同绑定 ACTIVE/epoch 15。
线上验收发现并修正了“尾部不足额预算导致误缩页”，完整记录保留中间版本证据。
S3 全历史扫描尚未完成，但有限分页持续推进；未强制造单、未验证本窗口新成交或盈利。
M1 仍未随此热修复发布，M2 静态定位状态不变。

优先把 `v2_settlement_precision_release_restart.py`、`v2_trend_release_restart.py`
对公共发布模块的全局变量赋值，改为不可变 `ReleaseSpec(prior, candidate, import_program)`。
参数必须贯穿数据库子进程 cwd、布局检查、链接替换和恢复核对，不能只改 CLI 外层。
暂不扩展成通用部署平台；服务级哈希/配置清单在这个窄接口上继续完善。

保留默认 dry-run、优雅退出、账户绑定及 epoch 校验、DRAINING/ACTIVE 顺序，
trend 的首阶段仍不自动恢复。新增同进程交错调用不同发布 spec 的隔离测试，
验证失败不会污染后续发布目标；不因重构增加自动回滚或强制恢复权限。
现有依赖闭包检查能检测 M1 两个新模块缺失，但正式候选包仍须实际核验。

### 删除和合并规则

1. “无静态 import”只生成候选，必须同时核对动态导入、测试、部署脚本、定时任务、
   恢复工具和外部调用。未部署的研究功能不自动等于废弃功能。
2. “服务 disabled”不等于实现废弃：managed controller 仍复用旧 daemon 装配和
   watchdog 健康能力；oneshot 任务完成后 inactive 是正常状态。
3. 数据库迁移、研究方法版本、发布验收 JSON、账户/结算恢复工具默认保留。
4. 研究任务不因整理目录而合并进交易主循环；复用代码不等于共享进程、预算或故障域。
5. 小步骤、单一行为目标、单独可回退。先纯结构变化，再单独评估策略变化，不能混做。
6. 既有未提交交易升级不包含在本次清理提交或发布中。未提交差异和服务版本差异
   必须记录，不能用覆盖或 reset 获得一个看似整洁的工作区。

### 本轮安全发现

- 已确认无引用的旧播报副本包含硬编码 Telegram 凭据且导入即执行。移除文件不能
  清除 Git 历史与旧仓副本；需要独立的凭据撤销/轮换。本轮未调用该 token、未修改 TG 配置。
- 另一个旧脚本会操作原工作树并自动 `git add -A` / push main，已从 V2 工具目录退役。
- 当前存在 144 个 release 目录和 4 套 virtualenv；部分 9 月 release 的配置仍被 Redis /
  ClickHouse 引用。它们属于引用感知的运维清理任务，不是本轮源码删除范围。
- 服务间版本不同并不等于错误，但工作区测试通过不代表线上组合已验收；后续发布
  必须验证真实入口和实际依赖版本。

---

## 历史升级路线（保留原始阶段记录）

> 2026-09-20 架构重置：后续以 [独立 V2 数据架构基线](V2_DATA_ARCHITECTURE_RESET.md)
> 为准。下文兼容旧行为、Redis authority 与文件恢复副本条目保留为历史记录，
> 不再约束新项目。已有完成状态不代表新架构完成；最终采用平仓后全新启动。

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
独立的 durable inbox/outbox PostgreSQL schema、纯状态模型与注入式 PG adapter
现已作为 dormant foundation 完成，并通过隔离临时 PostgreSQL 的事务和并发测试；
schema 尚未应用到持久或生产数据库，也没有生产 DSN、Telegram transport、API、
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
