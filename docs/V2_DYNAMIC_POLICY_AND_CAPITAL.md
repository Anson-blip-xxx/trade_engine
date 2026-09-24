# V2 动态策略与 Testnet 资金池改造

状态：资金模型与已接入配置已发布到 Testnet；全系统插件化仍按下方清单继续。
1000 USDT 具体公式和发布记录见 [资金演练方案](V2_CAPITAL_1000_REHEARSAL.md)。

## 资金口径

70% 是账户汇总保证金预算，不是每次从剩余余额再取 70%，也不是合约名义价值上限。
当前实现以 `min(totalWalletBalance, totalMarginBalance, availableBalance + totalInitialMargin)`
为实际资金上界；1000 USDT 模型另按已结算盈亏限制预算本金。新开仓须同时满足：

- 交易所已占保证金 + 尚未反映到采样中的预占 + 本次预占 ≤ 基数 × 70%。
- 尚未反映到采样中的预占 + 本次预占 ≤ 交易所可用余额。
- 本次预占 = 名义价值 / 经验证杠杆 × 可配置保证金缓冲（默认 1.10）。

`totalInitialMargin` 已包括挂单保证金，不能重复加 `totalOpenOrderInitialMargin`。
接口定义见 [Binance Account Information](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)。

原子预占在 PostgreSQL 账户锁内完成，发单前再读取交易所数据复核。
采样期间新增成交也保守计入预占。未知下单结果不释放资金；不能用超时假定撤单成功。
单笔金额由评分、波动率、止损风险及 allocation 上下限计算，而非固定 USDT。
数量上限和固定名义价值上限允许在 SANDBOX 的资金池启用后设为 null，LIVE 不允许。

## 已接入的动态判断

配置真源：`v2_business_state`，namespace=`runtime-policy-v1`，按完整账户环境隔离。
精确参数、默认值和范围以 `v2_core/runtime_policy.py::SCHEMA` 为准。

| 配置族 | 消费者 | 生效方式 |
| --- | --- | --- |
| capital.* | account_risk、managed_portfolio、account_context | 预占和发单前复核 |
| entry.*、score.*、leverage.*、stop.* | directional、directional_rules、replay | 新决策冻结完整配置 |
| sizing.*、analysis.* | directional、replay | 新决策冻结完整配置 |
| exit.* | directional_exit 服务及纯函数 | 每次退出评估记录版本、摘要与参数 |
| scheduler.* | trading_pipeline | 每轮重新读取 |
| health.* | trading_health | 每次诊断重新读取 |

更新必须提供 expected_version 和 reason；CAS 拒绝并发覆盖。配置变化不会重写旧决策。
修改配置不得导入任意 Python 模块、关闭幂等或绕过环境隔离。
CLI 为 `python -m services.v2_runtime_policy --account-id ACCOUNT schema|show|patch`；
patch 还须提供 `--changes JSON --expected-version N --reason TEXT`。
它不下单、不修改本地状态文件。启用演练时须先核对账户并记录钱包与时间锚点。

## 仍须完成的硬编码迁移与验收

以下是已定位的未完成项，不是已交付功能：

| 位置 | 未完成项 |
| --- | --- |
| s0/core.py、services/v2_s0_regime.py | 风险规避振幅、市场宽度、波动率分档及趋势强度 |
| s3/detector.py、v2_core/breakout.py | 事件阈值、突破确认、强度公式及突破状态机参数 |
| services/v2_trading_pipeline.py | 固定 S6/S8/source 组合改为经过验证的插件装配 |
| services/v2_testnet_daemon_entry.py | 扫描池、限频预算、数据时效、组件配置的统一来源 |
| services/v2_directional_account_context.py、services/v2_drawdown_context.py | 上下文时效、历史窗口、回撤参数 |
| v2_core/directional.py | 支持事件/市场状态规则与业务杠杆上限的进一步解耦 |
| 运维/展示 | 有权限的配置操作、配置回滚操作、配置版本与资金池占用展示 |

遗留未运行模块须另列清单；不能因为 V2 不调用，就声称其硬编码已经删除。
单位换算、协议枚举、数值类型校验、幂等唯一键是技术不变量，不是可任意关闭的交易策略。

本轮同时完成：历史覆盖审计改为同一事务快照内完整分页；跨币重叠交易逐笔现金归属和钱包核对测试；同币交易在上一单结算前不重入。

## 本次资金模型发布门槛

1. 使用已验证的现有 S6/S8 装配；其余上游配置/插件迁移不冒充已经完成。
2. 并发多订单不超预算；预算足够时可同时接受多个订单。
3. 其他币种持仓只有在账本、交易所数量和原生保护单吻合时才允许继续开仓。
4. 未知收入、缺失成交、时间边界歧义不能被忽略或强行结算。
5. 完整回归及多仓端到端测试通过后，才发布不可变版本并切换 Testnet 配置。
6. 观察自然信号从采集到结算分析的多仓实绩，而非人工直连下单代替验收。

发布过程只切换 V2 Testnet 服务和账号级配置，不切换 LIVE、不删除旧交易。

## QA 记录（2026-09-24）

- 账户/仓位/保护/退出/现金/健康检查的扩展回归：234 passed。
- 独立动态规则输出测试：4 passed。
- 独立配置与现金归属回归：33 passed；随后增加了预算充足接受 6 笔的用例。
- 首次全仓库回归：4292 passed，13 failed，10 skipped。
  13 项失败指向 S0 文件存储边界；日志实际加载的是旁边 main 仓库的代码。
  S0 单独测试为 152 passed。根因是遗留 S6/S8 将固定目录名 `trading_engine`
  插入导入路径，已改为自身仓库目录。
- 修复后组合回归（decision + S0 + 禁止文件回退 + 动态资金池 + 多仓保护 + 发单校验）：331 passed。
- 随后的全仓库回归：4327 passed、10 skipped、1 个既有测试返回值警告；首次的 13 项失败已解决。
- 最终资金停开告警、利润复投与双币结算追加回归：31 passed；同币互斥最终补丁定向回归 141 passed。
- Ruff 和 diff whitespace 检查通过；未将首次全量失败计作通过。

隔离测试使用临时 PostgreSQL/Redis，不连接运行账户数据库或交易所。
临时目录仅用于测试数据和诊断，不是交易系统的业务状态存储。
