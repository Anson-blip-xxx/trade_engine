# V2 数据核心：运行边界与验收

状态：开发中，不可上线。新入口是 `v2_core`，旧业务启动脚本不是其运行入口。
没有安装 systemd 服务，没有执行交易或在生产数据库应用 SQL。

## 数据职责与路径

PostgreSQL 是业务事实唯一权威：

1. `DecisionEvidence` 固化配置、策略版本、市场快照与开仓理由。
2. `TradingData.accept` 登记意图，复合请求键去重；同内容重试返回原意图。
3. `Orders.prepare` 预留 episode 和确定性订单身份。
4. `ExecutionRunner` 提交前 CAS；只有提交已确认的状态转换者能调用提交接口。
5. 交易所观察的身份验证、成交入账和结果更新在一个事务内完成。
6. `Ledger` 归集价差、手续费、资金费和修正；对账证据绑定账本 revision。
7. 同事务 outbox 分发不可变事件，消费者独立记录回执。

Redis 保存可重建的 trace 投影，不授权交易。`RedisTraces.rebuild_batch`
从 PG 分页重建，`data_revision` CAS 防止旧快照覆盖新快照。
重建必须与常规 outbox 消费一起运行，处理并发新增意图。
Redis 丢失不能通过“空仓”推断恢复交易。

ClickHouse 保存完整事件事实，查询 `v2_trade_events_logical` 去重视图。
原始 ReplacingMergeTree 可能保留多份物理副本，不能直接 sum 原始表。
重建使用新 consumer 名称重放 PG outbox，不能删除业务账本模拟重置。

本地文件仅限静态配置、诊断日志、测试产物。新数据核心不读写业务文件。
旧 `redis_store` 文件回退和 S0 文件双写已禁止；Redis 错误显式返回失败/异常。
这不代表旧模块的 Redis 全量覆盖写已完成迁移。

## 并发和故障语义

- 锁顺序统一为 intent 聚合根 → 子记录；同一交易的写入串行，不同交易可并行。
- 意图 UUID 不是业务幂等键。键范围包括交易所、账户、环境、产品、生产者、请求键。
- 成交 ID 还绑定 symbol；重复相同事实不重复计费，重复 ID 不同内容拒绝。
- 资金流水 source_id 在账户/环境/产品/kind 范围唯一，不能重复分配到两笔交易。
- 成交数量/价格和费用只接受十进制字符串，不使用浮点累计。
- PREPARED → SUBMITTING 的提交确认丢失：不发单，交给原身份查询。
- 请求超时：UNKNOWN；不能换 client_order_id 重试，也不能因查不到就假定失败。
- 未提交订单不得入账新成交；确认终态后只允许相同成交幂等重放。
- 下单结果和多条成交原子提交，非法第二条成交会回滚整次观察。
- PG 不可用：停止新增业务写入，不降级文件或 Redis 权威。
- outbox 至少一次投递。`run_scheduled_batch` 使用租约、令牌、指数退避（上限 300 秒）
  和每事件回执；失败不阻塞其他事件、不自动丢弃。下游必须按事件 ID 幂等。
  租约不能阻止慢消费者继续网络 I/O；它只阻止过期所有者写回回执。
- 直接 `run_batch` 是低层重放工具；同一 consumer 不应混用两种运行器。

## 盈亏与“为什么”

trace 包含触发时的指标、理由、配置版本，以及订单、成交、资金调整和结算证据。
对线性合约，净盈亏 = 方向调整后的成交价差 − 手续费 + 资金调整。
只有完整开平数量且费用币种可结算时才返回数值；CALCULATED 不等于 SETTLED。
SETTLED 需要明确的交易所平仓、订单终态、成交完整、资金完整证据。
迟到资金费新增账本 revision；旧结算保留，新 revision 重新核验。
财务分解可核验；策略理由是当时的决策证据，不可声称其证明盈利的因果关系。

当前只支持单 slot 活动 episode、一次开仓、多次部分平仓，以及 Binance 线性
USDT/USDC 结算。网格加仓、Polymarket、币本位及 FX 换算尚未完成，禁止据此上线。

## 依赖与只读排查

- Python 3.12（本次 QA 环境）、psycopg 3、PostgreSQL 16。
- Redis Lua CAS；本次 QA 使用独立 Unix socket，无持久化。
- ClickHouse 本地隔离测试验证 FINAL 去重；没有连接生产分析库。
- SQL 在 `db/postgres_v2_core_schema.sql`、`db/clickhouse_v2_schema.sql`。
  这些是新项目初建 schema，不是在线 ALTER 脚本；不得在已有生产 schema 重复运行。
- `V2_POSTGRES_DSN` 仅供只读 CLI `python3 -m v2_core <intent_uuid>` 使用。
  DSN 不写日志/报告；生产角色权限和连接池仍需部署接线。
- `SANDBOX` 缺省为开启，只有显式 `0`/`false` 关闭；`V2_PAUSE_OPEN=1` 暂停旧入口开仓。
  两者均为静态部署配置，不是动态业务状态。

## QA 重现

推荐从仓库根目录运行 `bash scripts/qa_v2_data_core.sh`，由脚本新建只监听 Unix
socket 的一次性 PG/Redis 并在退出时停止；不使用已有服务 DSN。
运行 `bash scripts/qa_v2_data_core.sh -q` 执行全仓测试。临时诊断目录保留，
无自动清理生产路径；要求 PostgreSQL 16、Redis、ClickHouse 和 Python 测试依赖。

默认 `PM_NO_WS=1 pytest -q` 不运行显式启用的数据库集成测试。
在专门创建的一次性 PostgreSQL 实例中，用独立 Unix socket DSN 设置
`V2_CORE_TEST_DSN` 和 `V2_CORE_TEST_ISOLATED=YES`；Redis 使用
`V2_REDIS_TEST_SOCKET`。然后运行 `pytest -q tests/v2_core` 或全仓回归。
每项 PG 测试创建随机 schema 并在 finally 清理，只允许一次性实例 DSN。
测试进程禁止 Python TCP/UDP 外连；这不是操作系统级 sandbox，C 扩展和子进程
仍需隔离环境与专用 DSN，不能把生产凭据放进 QA。

验收包括：并发幂等、内容冲突、不可变约束、提交确认丢失、超时查单、
成交重复/超量/错身份、整组回滚、部分平仓预留、精确盈亏、迟到资金费、
过期对账证据、outbox 重投/失败隔离、Redis 大整数版本和重建、CH 去重。
基础测试通过不替代真实协议接线和完整新运行环境端到端测试。

2026-09-20 检查点：启用隔离 PG/Redis/ClickHouse 的全仓回归
2984 passed、10 skipped、1 个既有测试返回值 warning；新核心 Ruff 通过。
修正了旧测试未替换真实 Redis/交易所读接口的隔离缺口，未放宽业务失败断言。
