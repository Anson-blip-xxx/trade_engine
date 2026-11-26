# V2 大表可恢复归档设计 · 2026-11-26

状态：源码依赖审计与后续验收设计，**不是运行库删除批准或已完成归档**。
审计基线 `0089ff8`；本次未运行大表扫描、运行库 DDL、删除、重启或交易请求。
容量时点与保留任务故障证据见 [磁盘审计](../V2_DISK_AUDIT_20261126.md)。
本文没有重新测量空间，也没有给出“可立即回收多少 GB”的未经核验数字。

## 1. 结论与优先顺序

1. 先恢复现有诊断保留查询的有界执行，再验收运行规模下的计划与耗时。
   `20261126_retention_reference_lookup.sql` 只补 S3 状态引用查找索引，
   不扩大删除白名单；隔离 QA 通过不等于运行库已经应用或达到时延要求。
2. 当前最大的 `v2_decision_evidence`、信号回执、S3 frame 等是证据和幂等链，
   **不能按“早于 30 天”直接删**。immutable trigger、硬外键和 JSON 软引用
   都需要保留；没有外键不代表没有业务引用。
3. 首期归档应只做“复制、校验、隔离恢复、只读查询”，源记录保持不变。
   为了马上腾空间而在剩余系统盘上复制整个数据库，会造成相反效果。
4. 后续分离“大体积不可变内容”和“小体积永久身份/证明”，先改读路径、
   再做并发/失效恢复验收，最后才讨论分批迁出。当前代码没有冷层读取契约，
   一份 dump 或一个摘要不能代替现有业务表中的完整证明。
5. 新增数据优先治理写放大：固定候选只写一次、配置按摘要去重、
   研究观察和控制状态分层。禁止为省空间原地改写历史快照或证据摘要。

## 2. 权威记录、可重建记录和当前删除资格

| 对象 | 职责与身份 | 当前判断 |
|---|---|---|
| `v2_decision_evidence` | `evidence_ref` 内容寻址；完整配置、观察与决策时点、特征、冻结计划/部署、候选或终态证明 | 不可变证据，不是缓存；成交、拒绝和未成交候选均可能引用。不能凭无 intent 判为垃圾 |
| `v2_state_history` | `(state_id,version)`、`(state_id,request_key)`；CAS 重试、状态演进、永久机会认领、账户覆盖证明 | 混合业务/诊断历史。只有第 4 节准确白名单可经现有保留函数清理；其他历史先归档设计 |
| `v2_business_state` | 当前状态、scope 和版本；有当前版本历史外键 | 运行权威。部分 payload 只是历史/coverage 内容引用，不能删旧 history 后只留下指针 |
| `v2_signal_receipts` | `(consumer,signal_id)` 唯一；INTENT / IGNORED / EXPIRED 及原因 | 永久消费结果，不是任务完成缓存。删除会失去重复消费证明；typed reader 还核对相关证据 |
| `v2_inbound_signals` | `signal_id`、`(source,environment,request_key)` 唯一；原始冻结 snapshot | 接收幂等根和审计输入。过期仅表示不能追单，不表示可以删除 |
| `v2_strategy_decisions` | `(consumer,signal_id)` 唯一；OPEN / IGNORED / EXPIRED 及 evidence | 决策结果。typed 准入并不要求预写 legacy OPEN，不能用“没有 decision”断言未准入 |
| `v2_s3_frames` | `(environment,frame_id)`；输入/config/发出事件和确切生命周期版本 | 生产与生命周期重试证明，不是可直接重算的 K 线缓存 |
| `v2_producer_batches` | `(source,environment,frame_id)`；内容摘要、观察时点、原 signal IDs | 生产幂等证明。重试相同 frame 应返回原信号，内容不同时应冲突 |
| `v2_strategy_tasks` | `(consumer,signal_id)`；lease、attempt、next_attempt、completed | 调度元数据，不是准入授权；但它是当前调度的规范记录，不能现在就当临时表删。完成任务可成为未来迁出对象，须先避免冷发现再次创建 |
| `v2_strategy_ready` | 未完成 task 的热投影，FK 到 tasks，触发器同步 | 可从 tasks 有界重建并校验。删投影不是删任务；enabled 投影的缺行会影响发现，不能在线任意清空 |
| `v2_strategy_entry_candidates` | `(consumer,signal_id)` first-writer；引用完整冻结候选 evidence | 不可变候选，不是 receipt/下单许可。尚未准入的候选也必须保持首次配置/输入，不能删除后按新配置重冻 |
| Redis / 派生报表 | 缓存、投影、读优化 | 只有明确具备权威来源和重建程序的具体 key/表才可重建；不作全库清空授权 |

`state_history` 的 request-key 唯一性与 `BusinessState.change()` 的
`ALREADY_APPLIED`/内容冲突判断相连；普通业务历史没有“只留当前版本就等价”的保证。
已有诊断保留规则是明确接受的窄例外，不可外推到永久机会认领、风控、
保护单、结算或账户覆盖证据。

## 3. 依赖闭包：硬引用和软引用都要闭合

### 3.1 源码确认的硬外键（箭头是引用方 → 被引用方）

```text
trade_intents ──(evidence_ref, config_digest, strategy_version)──> decision_evidence
trade_intents ──signal_id──────────────────────────────────────> inbound_signals
decision_evidence ──generated snapshot.signal_id──────────────> inbound_signals
strategy_decisions ──(evidence_ref, signal_id)─────────────────> decision_evidence
strategy_decisions / signal_receipts / strategy_tasks ─────────> inbound_signals
signal_receipts ──intent_id───────────────────────────────────> trade_intents
strategy_entry_candidates ──(evidence_ref, signal_id)──────────> decision_evidence
strategy_entry_candidates ──signal_id─────────────────────────> inbound_signals
strategy_ready ──(consumer, signal_id)────────────────────────> strategy_tasks
s3_frames ──(state_id, state_version)─────────────────────────> state_history
s3_frames ──(source, environment, frame_id)────────────────────> producer_batches
state_history ──state_id──────────────────────────────────────> business_state
business_state ──(state_id, version)──────────────────────────> state_history
```

最后一对是环；current-history 外键和 frame-batch 外键可延迟检查，
不等于允许永久缺失。`strategy_ready` 的级联删除只处理投影，
不赋予删除 task、receipt、signal 或 intent 的安全性。

成交依赖还延伸到 episode、order、fill、现金调整、收入分摊、FX、
资本台账、settlement、outbox/consumer receipt 和 outcome/followup。
不能仅导出上面 8 张大表就声称一笔交易可恢复。

### 3.2 必须由应用验证的软引用

- `producer_batches.signal_ids` 是 JSON 数组，无逐项外键；必须逐项对应原信号。
  S0 publisher 会以 `s3` source 接收信号，不能把 batch.source 与 signal.source
  生硬判等；应重用 producer 的真实映射和摘要算法。
- `s3_frames.emitted_events/config/input_digest`、lifecycle history 和 batch 的
  内容互相验证。保留 batch 行但删除 frame/history 不足以安全重放。
- `decision_evidence.snapshot` 的 plan、deployment、signal、duplicate proof、
  winner intent/evidence、candidate digest 等字段由 typed store/admission reader 校验。
  拒绝单也可能依赖另一信号的永久机会赢家，归档范围可能跨日期和 consumer。
- `strategy-opportunity-execution-v1` 当前状态与 version=1 历史携带
  `opportunity_id/intent_id/plan_digest/binding_digest/evidence_ref/signal_id`。
  `require_plan_admission()` 同时验证这些字段、原 intent、冻结 evidence、原 receipt。
  只保留一个 UUID 或只保留 hash 均不满足当前读取契约。
- `account-coverage-evidence-v1` 可使用 `_v2_state_history_ref`、
  `_v2_coverage_facts` 引用 history/coverage blobs；其他状态和研究结果也会引用
  episode、order、frame、evidence 或历史版本。不能仅靠 FK 反连接证明孤儿。
- PG candle delivery 持有 ClickHouse `archive_digest/input_digest/frame_id`；
  ClickHouse K 线及 manifest/chunk 是跨存储依赖，PG 快照不是它们的备份。
- 视图、SECURITY DEFINER 函数、动态 SQL、报表、研究任务、其他 schema 的外键
  也是读者。源码没有列出的运行库额外对象必须先盘点，不能自动忽略。

### 3.3 immutable 与权限边界

大多数证据表有 `BEFORE UPDATE OR DELETE ... v2_reject_mutation()`；
business_state 有禁止删除和版本递增检查，candle delivery 禁止删除/改终态。
retention role 仅在专用 state-history guard 的严格条件内例外。
因此“先导出，再按外键顺序 DELETE”在当前实现中仍不合法、也不能工作。

不得通过 `session_replication_role=replica`、禁用 trigger、超级用户绕过、
`TRUNCATE ... CASCADE`、直接删除 PG 文件或 DROP schema/partition 绕过保护。
未来任何迁出例外都要是新版本显式协议：验证完整归档证书、对象范围、
永久证明已存在、无运行引用，并以最小权限执行；不是给通用 DELETE 权限。

## 4. 现有保留策略能做什么

依据 `20261004_diagnostic_retention.sql` 与 `20261106_jev_health_retention.sql`，
仅以下 SANDBOX history namespace/reason 精确配对进入普通诊断候选：

| namespace | reason |
|---|---|
| `host-disk-health-v1` | `HOST_DISK_ASSESSMENT` |
| `testnet-trading-daemon-v1` | `TESTNET_TRADING_DAEMON_CYCLE` |
| `trading-health-v1` | `TRADING_BUSINESS_HEALTH` |
| `trading-pipeline-cycle-v1` | `TRADING_PIPELINE_CYCLE` |

同时必须：早于配置保留天数且 guard 最少 30 天；非当前版本；
没有任何 `v2_s3_frames(state_id,state_version)` 引用；以规定 retention owner
调用安全函数。选取 `FOR UPDATE SKIP LOCKED`、每批上限和超时仍必须保留。
其他 schema 的有效 FK 如存在会继续阻止删除；应报告阻塞原因，不绕过。

另有一个明确固定的 GLOBAL/JEV health identity（不是所有 GLOBAL 状态）：
`state_id=dffc450d-c51b-5def-a0d8-f1135a612573`，完整 scope 必须等于迁移中的
shared JEV ADVISORY scope；current/history 均非 deleted，reason 为
`JEV durable advisory coordination`，payload.schema 为
`jev-completed-model-health-v1` 且 `execution_authorized=false`，并满足同样的
年龄、版本和 frame 引用保护。不能仅按 namespace 选择所有 JEV 数据。

函数另外清理超过策略期限的 SANDBOX `v2_market_demands` 和过期 retention 审计行。
当前策略是诊断 30 天、过期需求 7 天、审计 365 天、每类每批 200 行，
服务最多 12 批；**不是整个库每批一共 200 行**。参数仍以实际 policy 核验为准。
其余七类大表和新候选表都不在当前清理授权内。

`v2_run_retention(false)` 不删除行且不写 `v2_retention_runs`（审计插入仅在
`p_apply=true`），但会获取 advisory/row locks，包括 `FOR UPDATE`。
本文没有调用它；未来预览应按维护任务权限执行，不能在 READ ONLY 事务
中运行后又声称已成功预览，也不能视为毫无锁影响的普通 SELECT。

## 5. 未来永久幂等最小记录：需要新契约，不是现在就能裁剪

建议采用“永久身份/承诺 + 可恢复不可变内容”的两层模型。最小身份集合至少包括：

| 身份层 | 永久保留的内容与行为 |
|---|---|
| 信号 | 原 source/environment/request_key/signal_id、规范 snapshot 摘要、版本及冷层定位；同 key 同内容返回原 ID，异内容冲突 |
| 消费结果 | consumer/signal_id、原 outcome/intent/reason、证明版本/摘要/定位；UNKNOWN 不能补成 EXPIRED 或成功 |
| 首次候选 | consumer/signal_id、首个 evidence/candidate digest 和完整冻结输入定位；新配置不能覆盖 |
| 准入机会 | account scope + opportunity_id 的永久赢家 intent；plan/binding/evidence/signal 证明及 version=1 认领；跨 producer/版本不能重复赢取 |
| 状态请求 | state_id/request_key/version、expected_version、payload/reason/deleted 的摘要与恢复位置；保留原 CAS 冲突语义 |
| 生产帧 | source/environment/frame_id、原输入/config/batch 摘要、signal IDs、lifecycle state/version、事件内容定位 |
| 交易/结算 | 原 intent/order/clientOrderId/fill/income/settlement 幂等键、资金账、成本及证据链，防止补发订单或重复入账 |

这张表是**下界设计要求**，并不证明字段已足够；尤其当前 reader 要读取并重算
完整 proof，而不是信任一个 `archived=true` 标记。迁出前必须有：

- 不可篡改、可验证的冷层内容；相同 identity 跨冷热只能有一个值。
- 运行库永久身份与冷层 manifest 的事务一致映射；导出成功但提交回执丢失可重试。
- 热读、冷读和双份同时存在时的等价结果；冷层丢失/校验失败显式 UNKNOWN，
  不生成新 identity、不复写 first-writer、不重复开仓/入账。
- 关键开仓/平仓恢复不能等待无限远端下载；活跃和未决证明留在热层。
  历史不可用不能堵住无关账户/合约，异常必须可观测。
- 身份归属含 schema/数据库代际、tenant/registry、AccountScope、consumer、source，
  明确每个字段所属域；禁止混淆 SANDBOX/LIVE 或把另一个账户的 proof 当作本账户。

## 6. 可执行的分阶段路线（本次仅设计）

### A. 元数据与对象白名单

冻结代码、迁移清单/hash、PG 版本、扩展、encoding/collation/timezone、角色/ACL、
RLS、函数/trigger/index 定义、所有 schema 的入站/出站 FK 及视图依赖。
验证实际对象与允许清单，不靠一个 search_path 或同名表认定身份。
未知 schema、外部引用、无效/未验证约束、禁用 trigger → 停止该归档批次。
这不暂停交易主流程，也不自动关闭数据库约束。

### B. 复制与恢复演练，不迁出

1. 先定独立归档目的地和容量预算：完整 dump、索引恢复工作空间、WAL、
   并发写入余量分别估算。不占满根盘；不假定压缩率。
2. 采用一致性快照导出 PG 元数据与选定数据闭包；全量备份和增量 cohort 是
   不同产品。长时间 MVCC snapshot 会阻止回收 dead tuples，必须限时并监测。
3. 每批 manifest 记录归档 ID、源 DB/schema/代际、snapshot/时间范围、
   精确主键清单、行数、每个文件与规范记录摘要、代码/迁移 hash、引用清单、
   完成状态。主键分页，不以业务时间/created_at 单调性假设漏掉迟提交。
4. 文件权限受限、加密保存及传输，密钥另存；备份可能包含加密账户凭据，
   不把 vault、配置或原行内容写到控制台/公开报告。
5. 恢复到隔离数据库/主机；应用网络禁止交易所、Telegram、模型服务，
   不安装执行控制器、不复制可用交易凭据。恢复 DDL 仅来自可信清单。
6. 校验所有约束与 immutable trigger、精确行数与规范摘要、软引用闭包，
   再运行第 8 节行为验收。最后形成“可恢复”证书；仅 dump 成功不通过。

### C. 首个可迁出 cohort

候选优先考虑“已完成调度历史”和“无成交且最终处理完的旧候选大体积内容”，
不是直接删交易账或 S3 状态历史。每个 consumer 都终态、无活跃 lease、
无待准备/UNKNOWN intent、无活跃订单/未核对收入/未完成 outbox/结算/研究引用，
且永久 proof 可读，才能成为候选。`completed_at`、过期或账户当前零持仓
均不是独立充分条件。

当前冷发现按 inbox 扫描后为缺失 task 重建调度。因此即使一个旧 task 完成，
直接删除仍会再次生成它；需先实现“已归档终态在发现/恢复中的等价判定”，
且不能把合法 INTENT 但未准备的任务误当终态。

迁出采用新协议的 claim → 导出 → 校验 → 恢复验收 → 永久证明登记 →
最终引用/活动状态复查 → 小批提交；任一步失败保留源记录。
最终复查与删除必须有并发 fence，防止导出后新引用接入。
实际批量大小和预算通过负载测试决定，不复用“诊断 200 行”当任意大对象预算。
删除只释放 PG 内部可复用页；物理缩容/重建需另估复制空间与锁，
不能把 DELETE 行数换算成 `df` 立即回收量。

### D. 时间分区与大对象规范化

适合先在全新非权威观察/诊断表采用时间分区；每个分区有固定 retention、
恢复清单、查询范围和跨分区唯一性设计。现有大表的身份通常不含时间：
`evidence_ref`、`signal_id`、`(consumer,signal_id)`、`(state_id,version)` 等。
不能简单把 created_at 加入主键就声称保持原全局唯一和所有 FK 语义。

若设计分区证据，应先有不分区的全局身份注册表，加带日期的内容层；
原 reader、引用 FK、同 key 冲突、跨分区查重/恢复都要迁移验证。
DROP/DETACH 分区不是 immutable row trigger 的安全替代；操作仍需证明无热引用、
保留永久身份、冷层可恢复，且检查来自其他 schema/分区的依赖。

可评估配置按 config_digest 分离、共享 market snapshot 按内容摘要存储、
时点/decision 小记录引用大内容，保持原规范哈希可重算；不改历史事实。
仅配置去重不代表 39 GiB TOAST 全能消失，需量化 config 与 features 各自占比。

## 7. 验收 SQL 设计（供后续工具使用，本次未执行）

运行库只允许有预算的 catalog 查询和已审查的主键点查。
`LIMIT` 只限制输出，不保证不扫描；先普通 EXPLAIN，**不先跑 EXPLAIN ANALYZE**。
不要把全表 COUNT、全库 JSON 递归或反连接“零结果”证明放进在线验收。
以下 `trade_v2` 是示例明确 schema；正式工具必须验证 schema OID/DB 身份。

### 7.1 所有 schema 的外键与 trigger

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '500ms';
SET LOCAL work_mem = '4MB';
SELECT current_database(), current_schema(), current_setting('server_version'),
       current_setting('TimeZone'), current_setting('transaction_read_only');

-- 不使用 connamespace=trade_v2 过滤：那会漏掉别的 schema 指向本 schema 的 FK。
WITH targets AS (
  SELECT c.oid
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='trade_v2' AND c.relkind IN ('r','p')
)
SELECT c.conrelid::regclass AS referencing_table,
       c.confrelid::regclass AS referenced_table, c.conname,
       c.convalidated, c.condeferrable, c.condeferred,
       pg_get_constraintdef(c.oid, true) AS definition
FROM pg_constraint c
WHERE c.contype='f' AND
      (c.conrelid IN (SELECT oid FROM targets)
       OR c.confrelid IN (SELECT oid FROM targets))
ORDER BY 1, 2, c.conname;

SELECT t.tgrelid::regclass, t.tgname, t.tgenabled,
       pg_get_triggerdef(t.oid, true)
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='trade_v2' AND NOT t.tgisinternal
ORDER BY 1, 2;
ROLLBACK;
```

还要通过 `pg_depend`/`pg_rewrite` 列出视图等依赖、通过 catalog 导出相关函数
定义并静态检查动态 SQL。catalog 无法证明应用层软引用不存在，
不能把上面查询当作“无引用证明”。

### 7.2 有界 cohort 检查示例

归档工具先生成小批 UUID 参数；下例只验证清单中记录，不做全表统计。
真正迁出前还需每个外键反向索引及软引用 reader 的验收。

```sql
-- $1 是已审查的有限 signal UUID 数组。只返回缺失或未完成的一小批异常。
WITH ids AS (SELECT unnest($1::uuid[]) AS signal_id)
SELECT ids.signal_id, 'SIGNAL_MISSING' AS violation
FROM ids LEFT JOIN trade_v2.v2_inbound_signals s USING(signal_id)
WHERE s.signal_id IS NULL;

WITH ids AS (SELECT unnest($1::uuid[]) AS signal_id)
SELECT t.consumer,t.signal_id,'TASK_NOT_TERMINAL' AS violation
FROM ids JOIN trade_v2.v2_strategy_tasks t USING(signal_id)
WHERE t.completed_at IS NULL OR t.lease_token IS NOT NULL OR t.lease_until IS NOT NULL;

-- 检查指定 state/version 的当前引用；S3 反向索引必须已验证可用。
SELECT b.state_id,b.version,'CURRENT_HISTORY' AS violation
FROM trade_v2.v2_business_state b
WHERE b.state_id=$1::uuid AND b.version=$2::bigint
UNION ALL
SELECT f.state_id,f.state_version,'S3_HISTORY_REFERENCE'
FROM trade_v2.v2_s3_frames f
WHERE f.state_id=$1::uuid AND f.state_version=$2::bigint;
```

这些结果为空**仍不足以批准删除**：还需完整 receipt/admission/candidate 校验、
所有 consumer、未决 intent、winner 软引用和其他 schema 引用；必须用主键
驱动有界探测或在隔离恢复库做全量闭包，不在运行库临时放大扫描。

### 7.3 隔离恢复库的完整验收

- 对 manifest 每个表精确 COUNT 与稳定排序的规范行摘要；JSON 使用当前
  canonical/hash 规则，binary COPY 摘要要记录 PG/格式版本，不跨版本想当然比对。
- 全量检测已声明外键、缺失约束、禁用 trigger、无效索引；不仅检查 row count。
- `producer_batches.signal_ids` 逐项类型/存在性/映射核验，S3 frame→batch→history
  内容重算；无法重算的旧格式明确 UNKNOWN，不把 hash 字符串相等当业务正确。
- `DecisionEvidence` 重算 evidence/config digest；`read_trade_plan()`、
  `require_plan_admission()`、`StrategyEntryStore.read()` 逐个证明冻结与原 receipt。
  仅恢复库启用的写测试用隔离副本，不能对只读归档本身重放 mutation。
- 检测 archive cohort 对外引用是否都由同批对象或永久热身份满足；
  incoming 与 outgoing 都要测，拒绝只做子表内部一致性。
- 恢复期间创建触发器前的数据导入是隔离恢复技术步骤，不能迁移为运行库
  绕过 immutable 的清理手段；最终 trigger/ACL 必须与可信基线一致。

## 8. 必须通过的行为与故障验收

1. 相同 signal/frame/request 重放返回原 identity；异内容同 key 冲突，
   不生成新 signal、candidate、intent、order 或资金分录。
2. 两个 producer、两个部署版本、两个 signal 同机会竞争只有一个永久赢家；
   loser 的 IGNORED 原证明恢复后仍可验证，不依赖最新配置。
3. 冻结后改配置、过期、停用部署、切账户，首次候选不漂移；
   有原 INTENT 的恢复不被误改为 EXPIRED/IGNORED；order-free UNKNOWN 不得 ACK。
4. 导出中断、文件缺失/截断、校验和不符、磁盘/网络不可用、manifest 提交回执丢失、
   归档 worker 被 kill，都不删除源数据；重试不跳过未确认批次。
5. 归档登记与并发新引用/状态更新竞争：要么保留完整热记录，要么读到完整冷证明；
   不出现半个事务、已归档但不可恢复、删除赢过新引用的窗口。
6. scheduler 重建投影、跨页冷发现、迟提交、重复投递、丢 ACK 后只恢复原任务；
   不重建大量已归档终态，也不漏掉已准入但尚未准备的 intent。
7. 交易账收益、手续费/资金费/FX、结算日/UTC+8 展示及报表在归档前后相同；
   不把历史缺失当成零或将 LIVE 行情与 SANDBOX 成交合成收益。
8. 在受限资源与百万/千万行代表性数据下验收 p95/p99、锁等待、WAL、I/O、
   查询计划与恢复时间。小样本 correctness 不代表容量验收。

## 9. 现有工具能复用和不能复用的部分

`scripts/v2_sandbox_rebuild.py` 可参考一致性 snapshot、受限目标命名、
schema dump、精确 checksum、目标数据库关闭连接的隔离手法。
但它只构建 **配置种子 SANDBOX 候选库**，没有全量交易备份/恢复能力，
输出明确 `backup_restore_verified=false`，不是本方案的归档工具。
其严格表/namespace 白名单尚不包含新候选等后续对象，遇未知对象应拒绝；
不能为了“让脚本运行”删除白名单检查或直接替换生产库。

本次交付到此为止：依赖边界、实施顺序和验收标准。运行数据保持原样。
下一可独立开发单元应是**只导出+manifest+隔离恢复验证**，不带 source DELETE，
完成后再评审冷热 proof reader、永久身份表和有界迁出协议。

## 源码依据

- `db/postgres_v2_core_schema.sql`：证据、交易、状态、信号、任务、批次/frame 的 DDL。
- `db/migrations/20261126_strategy_entry_candidates.sql`：首次冻结候选与不可变保护。
- `db/migrations/20261004_diagnostic_retention.sql`、`20261106_jev_health_retention.sql`：保留白名单与 guard。
- `db/migrations/20261126_retention_reference_lookup.sql`：引用查询索引，不授予新删除权限。
- `v2_core/evidence.py`、`state.py`、`producer.py`、`lifecycle.py`：哈希、CAS、生产重放语义。
- `v2_core/strategy_plan_admission.py`、`strategy_signal_admission.py`、`strategy_entry_store.py`、`strategy_prepare.py`：完整准入与恢复证明。
- `v2_core/scheduling.py`、`db/migrations/20261110_strategy_ready_queue.sql`：规范任务与可重建热投影。
- `v2_core/coverage_evidence.py`、`market_archive.py`、`chunked_archive.py`：大对象和跨存储证据。
- `scripts/v2_sandbox_rebuild.py`：只建配置候选库的范围和失败关闭机制。
