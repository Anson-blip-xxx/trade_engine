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

当前每个 slot 保持一个活动 episode；同一 episode 可分配多笔 MARKET/LIMIT 开仓，
累计已成交加待成交预留不得超过意图 quantity。quantity 是本周期累计开仓上限，
不是可无限重复使用的净仓上限。部分平仓只使用已确认成交量并扣除其他平仓预留。
取消一笔挂单不能释放仍有持仓或其他挂单的 slot；订单价格、类型、有效方式和
逐单理由不可变。网格策略的实际生产入口接线仍未完成。

盈亏目前限 Binance 线性 USDT/USDC 结算。未在本仓库发现 Polymarket 适配代码，
这里的 pm 指持仓管理模块；没有新增 Polymarket 或币本位支持。非结算币种费用
尚需带证据的换算，当前明确保留 PENDING。

## 信号、策略状态及恢复入口

- `Signals` 把规范化信号存入 PG，不保存 webhook secret；来源/环境/请求键固定身份。
  同键不同内容拒绝。市场逐笔原始数据仍属于行情层/CH，不能全部挤入交易信号表。
- `TradingData.accept_signal` 将证据、意图、outbox 和消费者回执原子提交。
  同一账户/环境/产品/策略对同一信号最多一个意图；更换请求键也不能绕过数据库约束。
  信号驱动意图使用 `signal:<signal_id>` 请求键。被忽略/过期的消费结果不可复活。
- `BusinessState` 用于非账务策略状态：账户范围、版本 CAS、请求键幂等、不可变变更历史、
  删除留存 tombstone。提交时数据库检查当前版本必须有历史记录。它不是账户资金预留
  的替代品；涉及资金和订单的关联操作必须放在同一领域事务内。
- `DataRuntime.accept_open` 分开登记与提交，`prepare_initial=False` 可登记网格计划后
  再分配子订单。`expires_at_ms` 属于不可变决策证据；入队和发单前都检查有效期。
- `DataRuntime.tick` 顺序执行过期清理、查询恢复、超时事件和投影；不会提交新订单。
  PREPARED 可过期取消；SUBMITTING/UNKNOWN 只查询，不能因超时假定没有成交。
- `RecoveryAttention` 每个订单版本生成一次持久告警事件。`AttentionNotifications`
  注入通知接口，过时事件不再提示；通知带事件 ID，不带可直接执行的审批指令。
  TG 传输尚未配置；确认丢失时通知可能重复，不能承诺 exactly-once。
- `connection_factory` 强制独立 schema、同步提交、连接/锁/语句超时；CLI 使用只读事务。
  `V2_POSTGRES_SCHEMA` 缺省 `trade_v2`，拒绝 public/系统 schema；`--pnl-currency USDT`
  可只读查询财务报告。数据库角色最小权限仍须在部署阶段配置。

## 依赖与只读排查

- Python 3.12、psycopg 3.3.6、PostgreSQL 16；已测试的顶层依赖固定在
  `requirements-v2-data.txt`，测试依赖在 `requirements-v2-data-qa.txt`。
  这不是完整的跨平台传递依赖 hash lock，生产镜像仍需固定系统包/镜像摘要。
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
可用 `V2_QA_PYTHON=/path/to/venv/bin/python3` 明确选择测试解释器。

默认 `PM_NO_WS=1 pytest -q` 不运行显式启用的数据库集成测试。
在专门创建的一次性 PostgreSQL 实例中，用独立 Unix socket DSN 设置
`V2_CORE_TEST_DSN` 和 `V2_CORE_TEST_ISOLATED=YES`；Redis 使用
`V2_REDIS_TEST_SOCKET`。然后运行 `pytest -q tests/v2_core` 或全仓回归。
每项 PG 测试创建随机 schema 并在 finally 清理，只允许一次性实例 DSN。
允许的 socket 路径仅 `/tmp/v2-data-qa.*` 或本任务早期 `/tmp/codex-v2-data-qa.*`。
测试进程禁止 Python TCP/UDP 外连；这不是操作系统级 sandbox，C 扩展和子进程
仍需隔离环境与专用 DSN，不能把生产凭据放进 QA。

验收包括：并发幂等、内容冲突、不可变约束、提交确认丢失、超时查单、
成交重复/超量/错身份、整组回滚、部分平仓预留、精确盈亏、迟到资金费、
过期对账证据、outbox 重投/失败隔离、Redis 大整数版本和重建、CH 去重。
基础测试通过不替代真实协议接线和完整新运行环境端到端测试。

新增恢复演练：pg_dump/pg_restore 到另一临时数据库后，轨迹、账本与缓存重建一致；
只针对脚本创建且 `cluster_name=v2_isolated_qa`、fsync=on 的临时实例执行崩溃重启。
已提交交易保留，未提交交易消失，恢复后不盲目重发。崩溃测试仅串行运行。

2026-09-20 检查点：启用隔离 PG/Redis/ClickHouse 的全仓回归
2984 passed、10 skipped、1 个既有测试返回值 warning；新核心 Ruff 通过。
修正了旧测试未替换真实 Redis/交易所读接口的隔离缺口，未放宽业务失败断言。

后续检查点：新核心 74 passed；全仓 3011 passed、10 skipped、同一既有 warning。
在独立 virtualenv 中安装 psycopg 3.3.6 后，全仓结果一致。初次系统环境驱动为
3.1.17，不符合旧 requirements-postgres.txt 的 >=3.2 要求，未修改系统安装。

协议适配检查点：全仓 3041 passed、10 skipped、同一既有 warning。
`v2_core.binance.BinanceFutures` 通过注入的认证 transport 对接 USD-M 单向模式，
绑定账户/环境；提交只调用一次，恢复只查单，分页成交不完整不宣告终态。
LIMIT 条件、reduceOnly、客户端/交易所订单身份均核验。没有建立真实 HTTP 连接，
transport 必须有超时且不能自动重试写请求；签名传输和完整部署仍未接线。
协议依据：[Binance 官方 USD-M REST Trade 文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)。
历史订单查询具有保留期限，因此 not-found 不等于未成交，也不允许重发。

同笔成交跨 REST/实时推送到达时，财务字段一致只计一次账；不同来源证据
追加到不可变 `v2_fill_observations`，不增加 accounting_revision。
费用允许交易所明确报告的负值（返佣），不得在无证据时自行推导。
UNKNOWN 状态首次查到交易所订单号仍会持久绑定，状态未变化不能丢失身份。

历史估值检查点：全仓 3043 passed、10 skipped、同一既有 warning。
`TradingData.valuations.record` 为具体成交费用/资金调整记录带来源和理由的
历史汇率（目标币数量/一单位原币）。不执行换汇，不声称是现金兑换成交。
报价与原事实时间差必须在调用方指定范围内，硬上限五分钟；缺失时报告返回
missing_valuations 和 PENDING，不把未知费用当零。生产历史报价源尚未接入。
每条估值使用 request_key 幂等和 expected_version CAS，修正只追加新版本；
增加 accounting_revision，使旧结算证据失效。报告标注 HISTORICAL_MARK。
原币金额和所有旧报价始终保留；数据库约束拒绝跨 episode/币种挂接。

传输/恢复检查点：全仓 3067 passed、10 skipped、同一既有 warning。
`BinanceSignedTransport` 提供 HMAC-SHA256 签名、固定环境域名、默认禁写、
有限 socket 超时与响应大小限制；没有自动重试和重定向，错误信息不包含
响应正文或签名 URL。必须注入共享限流许可，不能把各进程的独立计数当全局限流。
依据：[Binance 官方 General Info](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)。
此处只用假连接测试签名和错误行为；没有真实请求，也没有生产启用。
环境标签 SANDBOX 在该 transport 中是 Binance 测试网，而非无网络模拟器。
测试模拟仍依赖注入假连接；不要给 QA 提供任何真实凭据。

批量恢复使用 `v2_order_recovery` 持久排期、租约与失败退避，避免长期未知订单
挡住队列后面的订单。租约过期可能重复查询，但不会产生重新下单许可；
结果提交依旧依赖账本幂等与订单身份。风控通过证据在 SUBMITTING 提交时保存，
提交成功后才调用交易所。完整跨账户风控预留策略仍属于实际入口接线门禁。

自动收尾检查点：全仓 3069 passed、10 skipped、同一既有 warning。
`SubmissionNotSent` 仅允许固定的“写请求前已阻止”原因：模式预检查失败、
不支持的账户模式、写入禁用或限流拒绝。此时持久 REJECTED，并释放无成交的
episode；相同意图不能复活。POST 已尝试后的超时、5xx、解析失败仍保留 UNKNOWN。
本轮所有测试均使用隔离数据库或假交易所连接；测试服务退出时已停止。
整项数据架构迁移仍未达到发布门禁，不能把这些回归结果视为全策略接线验收。

## 资金流水持久入口

`BinanceIncomeImporter` 只调用 GET `/fapi/v1/income`，要求明确的账户、环境和
历史时间窗。协议依据：[Binance 官方 Income History](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)。
流水身份以账户范围 + incomeType + tranId 去重，而不是只用时间戳或流水号。
本实现保守限制单窗七天、最近八十天；更早数据需要独立归档导入流程。
这是代码导入策略，不是交易所接口的时间窗限制。

- `v2_exchange_income`：不可变原币事实和规范化来源证据，不直接等于交易盈亏。
  没有把自由文本 info 当作必要账务身份，也不会把凭据写进证据。
- `v2_income_imports`：RUNNING/FETCHED/PARTIAL/FAILED 与已提交页数/行数。
  本页事实和进度同事务；页面错误整页回滚，前页保留。提交回执丢失后从 PG
  读取实际进度。进程中断留下 RUNNING；新运行从第一页重放即可去重，不跳时间游标。
  页数耗尽、重复/移动分页不会被标为 FETCHED。生产调度必须重叠回扫迟到流水。
- FETCHED 仅表示本轮分页取完，不证明未来不会迟到、不等于 cash_complete，
  不自动触发 settle。终结运行结果和查询范围不可改写。
- `TradingData.income.pending(scope, income_type="FUNDING_FEE")` 查询待分配资金费；
  不设置过滤则保留所有待核验类型。此接口是有限查询，不是自动分配调度器。
- `assign_funding` 要求相同账户/环境/产品/合约、当前 accounting_revision 和对账
  来源证据；订单必须终态、交易已闭合、费用时刻可证实有持仓。与成交同毫秒的
  边界归属拒绝猜测。非 FUNDING_FEE 禁止入资金费账，避免佣金/价差重复统计。
- 分配标记、现金账本、会计版本和 outbox 同事务。失败全部回滚；并发分配只记
  一次。trace 返回来源和分配证据；迟到资金费会使旧结算版本失效。
  这是给自动对账器使用的内部契约，不要求用户逐笔人工审批。

本轮全仓 QA：3087 passed、10 skipped、1 个既有 warning；新核心 Ruff 通过。
包括分页中断/重放、提交回执丢失、财务重复计账防护、跨账户隔离、并发分配、
事务故障回滚及含未分配流水/导入进度的真实 PG 备份恢复。
没有连接实际交易所；生产定时采集、自动归属器与完整账户核对仍需后续接线。

## 策略决策入口

详见 [入口契约](V2_STRATEGY_ENTRY_CONTRACT.md)。`StrategyWorker` 已连接 PG 信号、
不可变决策、意图登记与订单准备。首个持久决策胜出，重试复用原配置和期限；
函数本身必须无副作用。拒绝与过期通过只读 `--decision` 查询，不需要存在订单。
等待合约容量不会延长决策有效期。旧策略入口、批量消费调度和保护单未在本轮切换。

本轮全仓 QA：3107 passed、10 skipped、1 个既有 warning；新核心 Ruff 与
git diff --check 通过。测试只使用独立临时 PG/Redis、ClickHouse 本地实例及模拟
交易所端口，隔离测试服务已停止。该结果不等于生产切换验收。

## 持久策略调度与贯穿 QA

`v2_core.scheduling.StrategyScheduler` 新增 PG 策略任务排期、公平发现、逐条租约
领取、失败退避及旧令牌隔离。容量等待不是完成，原期限到达则终止；恢复已存
决策不依赖重新读取行情。该接口只准备订单，不提交订单，未安装常驻调度服务。
接口边界见 [策略入口契约](V2_STRATEGY_ENTRY_CONTRACT.md)，整项剩余工作见
[验收清单](V2_REMAINING_WORK.md)。

新增 17 项 QA（含参数化用例）：坏信号不阻塞后续、容量等待恢复/过期、未来信号、
过期时绕过行情源、并发领取、租约回收、旧令牌隔离、决策后崩溃、参数边界、
调度确认提交前失败/提交后回执丢失、策略/来源/环境隔离，以及盈利/亏损贯穿验证。
贯穿测试连接真实临时 PG/Redis 和 Binance 协议适配器，验证丢响应后只查询原单、
开平仓成交、费用、资金费、结算与缓存清空后重建。交易所响应、风险策略和结算
核验结论由 QA 注入，不是实盘或全部生产链路的验收。

最终全仓结果：**3124 passed，10 skipped，1 个既有 golden test 返回值 warning**。
Ruff 和 diff 空白检查通过；QA 临时服务已停止，诊断目录为
`/tmp/v2-data-qa.0E3tLo`。未执行真实交易、运行数据库更新、生产服务重启或环境清理。
