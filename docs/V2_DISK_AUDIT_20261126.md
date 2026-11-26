# V2 磁盘审计 · 2026-11-26

采集：14:46–14:52 UTC+8；只读目录统计、PG catalog/计划、维护日志。
本轮未删除文件、交易证据、数据库记录或索引，未对运行库执行 DDL、
VACUUM FULL、重启或交易请求。执行控制器、旧测试交易守护及 watchdog
均为 inactive/dead，继续遵守暂停开仓要求。

## 结论

- 根盘 295.2 GiB，已用 212.2 GiB，可用 70.8 GiB（`df` 显示 75%）；
  inode 使用 3%。不是立即写满，但需要控制持续增长。
- V2 PostgreSQL 数据目录约 109 GiB，几乎都在 `base`；WAL 约 129 MiB。
  主要问题不是 WAL、临时 QA 或日志。
- 定时诊断保留任务已失败数日。缺少 S3 状态引用的反向查找索引，
  引用保护查询全扫约 1,039 万行，超过现有 5 秒语句预算。
- 修复该查询只恢复现有小范围保留能力，**不能回收整个 109 GiB**。
  大部分表根本不属于当前删除白名单，需要另做可恢复归档及依赖审计。

`df -B1 /`：总量 316,933,189,632；已用 227,887,099,904；
可用 76,037,849,088 bytes。数字是一个时点，不据此猜测剩余可用天数。

15:11:37 UTC+8 完成 QA 后复查：已用 227,944,964,096 bytes、可用
75,979,984,896 bytes（约 70.8 GiB）；`df` 四舍五入显示 76%。
QA 临时数据库已由隔离 harness 回收，运行数据未删除。

## 空间归属

| 路径 | 近似占用 | 处理判断 |
|---|---:|---|
| `/var/lib/postgresql/16/tradev2` | 109 GiB | V2 主数据，禁止删除 PG 文件 |
| `/home/ubuntu/meme/data` | 41 GiB | 其他项目，不纳入 V2 清理 |
| `/home/ubuntu/.local/state/polymarket-marketlab` | 17 GiB | 其他项目，不纳入 V2 清理 |
| `/home/ubuntu/.codex/sessions` | 8.2 GiB | 会话历史，不擅自删除 |
| `/home/ubuntu/grok/meme/data` | 4.7 GiB | 其他项目，不纳入 V2 清理 |
| V2 独立 ClickHouse | 4.8 GiB | 其中约 4.4 GiB 是增量 K 线，不当作垃圾 |
| `/opt/trade-engine-v2` | 2.2 GiB | 发布包约 2 GiB，需保留正在使用及回滚版本 |
| `/var/log` | 1.2 GiB | 非主要来源；journal 约 266 MiB |
| `/tmp` | 66 MiB | 没有堆积的大型 QA 数据库 |

目录统计有包含关系，不能把所有行直接相加。没有检查其他项目的数据内容。

## PostgreSQL 大表

以下 total 包含索引、TOAST 和附属 fork；行数为 `pg_stat` 估计，非全表计数。

| 表 | total bytes | 估计行数 | 说明 |
|---|---:|---:|---|
| `v2_decision_evidence` | 44,825,772,032 | 3,605,100 | TOAST 41,592,119,296 bytes，宽决策快照是最大项 |
| `v2_state_history` | 27,672,444,928 | 17,509,301 | 业务/诊断历史混合，不能整表清理 |
| `v2_signal_receipts` | 10,455,236,608 | 17,154,215 | 幂等回执；索引 7,362,813,952 bytes |
| `v2_inbound_signals` | 8,990,343,168 | 8,902,523 | 原始信号及引用根 |
| `v2_s3_frames` | 8,508,801,024 | 10,394,427 | 状态版本引用和行情生产证据 |
| `v2_strategy_tasks` | 5,828,313,088 | 16,911,374 | 任务历史，完成任务不等于可以删回执 |
| `v2_producer_batches` | 3,440,566,272 | 10,448,866 | 生产批次幂等身份 |
| `v2_business_state` | 2,706,825,216 | 637,273 | 当前业务状态，保留 |
| `v2_strategy_decisions` | 2,541,273,088 | 3,606,209 | 决策身份及证据引用 |
| `v2_positioning_observations` | 1,087,438,848 | 59,699 | 研究数据，归档前仍需核对引用 |

## 保留任务故障证据

- `trade-v2-diagnostic-retention.service` 最近 14:02:30 启动，14:02:35 失败。
- `/var/log/postgresql/postgresql-16-tradev2.log` 的
  `2026-11-26 14:02:35.863 CST` 记录 QueryCanceled：
  `v2_run_retention` 第一条诊断候选 SELECT 超时，尚未开始删除。
- 原样 `EXPLAIN`（不是 `EXPLAIN ANALYZE`）包含 Parallel Hash Right Anti Join
  和对 `v2_s3_frames` 的 Parallel Seq Scan。现有主键是
  `(environment,frame_id)`，不能直接支持 `(state_id,state_version)` 引用查询。
- `v2_retention_runs` 最后成功为 run 706，
  `2026-11-20 11:56:17.443256+08`；当时 diagnostic_removed=0、batch_full=false。
- policy：启用；诊断 30 天，过期需求 7 天，每批最多 200 行，审计至少 365 天；
  服务最多 12 批。不是错误配置了“禁用”。
- QA GC、旧轮转日志 GC 正常，近期均回收 0；只读预览也没有大的合规删除目标。

## 修复及容量治理顺序

1. 准备 `(state_id,state_version)` 索引迁移；在线脚本使用 CONCURRENTLY，
   拒绝同名错误/无效索引，不自动 DROP。保持 5 秒预算和所有引用保护，
   不用放宽超时掩盖全表扫描。**本轮仅源码与隔离 QA，不代表运行库已修复。**
   文件：`db/migrations/20261126_retention_reference_lookup.sql`（空库/离线迁移），
   `deploy/v2-testnet/retention-reference-index.sql`（明确运行库、5 秒锁等待、
   单语句 15 分钟、64 MiB 构建内存、禁并行的在线脚本）。
2. 应用后需要用运行库实际计划和保留服务成功记录验证，检查锁等待、I/O、
   索引额外空间。隔离小样本通过不能替代千万行规模验收。
3. 新策略先持久化一份冻结候选，重试复用同一身份和输入，不随重试复制快照。
   新增存储有明确大小上限；不改旧数据的完整性规则。
4. 大表另建归档方案：区分已成交/已准入/永久机会认领、未成交诊断、
   可重建投影；保留完整交易引用链与小型永久幂等凭据。
   先生成清单、校验和、恢复演练及依赖核验，再讨论有界清理。
5. 删除 PG 行通常只腾出数据库内部可复用页面，不会让 `df` 立即下降。
   不安排全库 VACUUM FULL，也不在当前盘上无预算复制百 GB 归档。

需要形成后续容量证据：各大表每日 bytes/行数增量、保留运行成功时间、
候选/实际删除行数、完成任务比例与索引膨胀。尚未取得可靠连续增长序列，
因此本次不宣称“还能用多少天”或“清完可释放多少 GB”。

验证范围：保留规则与索引相关 5 文件共 130 项隔离测试通过；包含 24,000 条
无关 S3 frame 的代表性样本，未强制优化器选择索引，候选查询选择新索引，
完整 preview/apply 在原 5 秒预算内完成。它验证功能和这份样本，
**不代表千万行运行库已经完成性能验收**。没有直接清库，没有扩白名单。

最终与消费链路合并回归：25 文件 787 项通过，0 失败；详情见
[QA 记录](qa/20261126_typed_entry_disk_retention.json)，重叠测试不累加计数。
