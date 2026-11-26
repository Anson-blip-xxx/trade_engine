# 新策略常驻组合与暂停启动

## 授权边界

继续执行用户的“先暂停开单，把新东西弄好”。本轮启动的目标是 SANDBOX
暂停管理模式，不是恢复开仓，不是 LIVE 发布，也不以测试通过证明策略盈利。

固定链路：目录结构观察 / TV / S3 → 已收盘行情与因果结构 → 真实账户事实
→ 受限仓位计算 → 不可变候选和计划 → 独立入场执行 → 独立仓位管理
→ 独立财务结算与分析。模型和历史旧策略不处于这条新链路的必经路径。

## 本轮接线

- `TYPED_STRATEGY` 是固定运行模式，不允许从配置导入任意函数。
  entry、management、settlement 各自有 owner 和截止时间；入场故障不能阻塞保护、退出。
- 控制器只在所有本地子进程停止确认后放弃所有权；未知订单沿用原身份核对，
  不按“可能没成交”重新发单。DRAINING 允许管理，拒绝开仓。
- 明确选择 `V2_TYPED_RECOVER_PAUSED=true` 时，只恢复同一 STOPPED 账户至 DRAINING，
  不生成切换请求、不增加 route epoch、不变更账户、不转为 ACTIVE。
- 账户事实使用有界只读 Testnet API；杠杆支持 2/3/5，未证明高杠杆条件时不提供 8 倍。
  保证金占用包括实际缓冲。旧的尚未迁移风控配置显式拒绝，不暗中取消。
- 独立 ClickHouse 用户仅可本机读取分钟归档，限制时间、行数、字节和线程；
  缺分钟或重复冲突不当作有效行情。全目录扫描持久轮转，不固定截断少数币种。
- 新策略实例从首次不可变部署的有效信号窗口开始订阅，避免给所有旧信号
  重新制造 EXPIRED 审计和领取任务。已有候选、回执、任务、决策和意图独立恢复；
  generation 升级不重置边界。该边界不是历史存储清理方案。
- typed 健康信息不借用旧 S6/S8 心跳证明正常；保留实际订单、残仓、结算检查。
  未完成的队列全覆盖标为 UNKNOWN/NOT_EVALUATED，不把进程存活叫作交易健康。
- typed 开平仓通知使用真实计划、保护与结算证据，兼容旧消息投递幂等。
  模型评分不存在时明确不展示传统评分。通知异常不阻塞财务结算。

## 发布先决条件与启动方式

1. 保留原 release 和配置；核验用户暂停意图、当前 route 与实际账户一致。
2. 安装缺失的五张候选/发现/维护/扫描/派发小表，持有 STOPPED route 锁并使用短锁超时。
   不修改既有交易、结算或账户风控；表不存在才创建。
3. 老服务权限用 `execution-guard-scope-upgrade.sql` 补三个范围校验列的 SELECT，
   不能授予凭据或激活权限。完整新安装使用原 grants 文件。
4. 安装 `strategy-reader-clickhouse.xml`，仅热加载只读用户，不重启归档服务。
5. `causal-trend-v1.json` 是待验证假设，不是盈利参数。首次发布为 OFF，保留不可变历史。
6. 使用实际 controller venv/user 和合并后的 service 环境运行
   `python -m services.v2_strategy_readiness --symbol BTCUSDT`。
   `paused_boot_ready` 仅证明启动先决条件；`entry_ready` 明确保持 false。
7. 将 `typed-strategy.env.example` 安装到外部 `/etc/trade-engine-v2/typed-strategy.env`，
   最后加载 `99-typed-strategy.conf.example`。两项 entry 标志都必须 false。
   `EnvironmentFile` 会覆盖 `Environment=`，不能只加一个 `Environment=false` 自认为安全。
8. 使用不可变正式 release，切换 controller 链接并启动。只重启 controller，
   不重启数据库/行情/看板、不使用会自动发 ACTIVE 请求的旧发布脚本。
9. 验证 DRAINING、管理与结算进度、关闭的 entry 标志、无新增 OPEN，再做正常重启复验。

回滚应先停 controller 并确认整个 cgroup 退出，再恢复上一个 release；
**保留双 entry=false 覆盖文件**。旧代码不能理解新模式时保持停止，不能顺手打开旧交易。
新表、只读用户和最小列授权无需删除。不得清理未知订单或结算事实来让验收通过。

## 验收边界

隔离 PG + 假 HTTP 的贯穿测试覆盖 BUY/SELL、真实事实 provider 组合、丢开仓响应后查询恢复、
冻结计划退出、保护单、成交与费用、结算额度释放和两条生命周期通知。
它不是实际 Binance 撮合、真实网络故障或多日盈利验收。

相关整合回归 2,635 项通过；最终启动待切换请求和通知边界修正后，另跑 151 项专项全部通过。
两组包含重叠，不相加；后一组不是全仓重跑。最终专项前后变更 Python 摘要一致，
Ruff/diff 检查通过，QA 数据库已回收。详见
[可核查 QA 记录](../qa/20261126_typed_runtime_paused_startup.json)。

真实只读检查已验证实际 Python、表/权限、依赖和专用行情用户，BTC/ETH 各 1440 根
连续已收盘分钟线可读。部署启动后的实际证据另记，不用前述测试代替。

仍需后续独立完成：全队列业务健康观测、自然信号的授权 Testnet 撮合闭环、
多日容量/故障验收、共同机会成本后样本外对照；在此之前不宣称恢复交易或具备盈利优势。
历史归档与永久幂等证明继续按 DATA_ARCHIVE_PLAN_20261126.md 实施，不能直接删交易库。
