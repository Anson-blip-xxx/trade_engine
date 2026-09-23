# S6/S8 真实账户上下文

## 已实现

`services/v2_directional_account_context.py` 为方向策略生成只读、账户绑定且先持久化的
仓位计算输入。它只接受 `BINANCE / SANDBOX / FUTURES`，不会发送、撤销、保护或平仓。

每次采集依次使用以下权威来源：

- Binance 签名 `positionRisk` 前后快照：发现目标标的已有仓位或采集期间任意持仓变化
  即失败；其他标的保证金仍计入账户已用额度。
- Binance 签名账户：`canTrade`、钱包余额、可用余额、持仓初始保证金和挂单初始保证金。
- 账户级 `symbolConfig`：目标标的最大名义金额。
- 公共 `exchangeInfo`：永续合约状态、结算资产、市场单数量步进/上下限、价格 tick 和
  最小名义金额。
- 公共 `premiumIndex`：有明确时间戳的当前资金费率。
- 公共 `globalLongShortAccountRatio`：1 小时维度、3 条有界请求中的最新空头账户比。
- PG `DrawdownState`：由本次真实钱包余额证据推进的持久回撤系数。
- 显式历史端口：14 日统计和来源证据；缺失、过期或字段不完整不会退化成零历史。
- 原始 S3 信号：PULSE/PUMP/PANIC 使用 `chg_15m`，TREND 使用 `chg_1h`，VIOLENT 使用
  `vol_1h` 生成预期幅度，不刷新也不从其他行情猜测。

账户、规则、公共行情和历史响应都以摘要进入
`directional-account-context-v1` PG 状态；标准化的余额、保证金、规则、资金费和多空比
随状态保存。随后才更新回撤观察并返回上下文，决策证据引用两个 PG 状态版本。

## 失败关闭

以下情况不会返回可供策略使用的上下文：账户不可交易、目标已有仓位、前后持仓变化、
余额/保证金非法、规则缺失或重复、非交易中永续合约、错结算资产、公共数据错标的/
越界/过期、历史统计缺失/过期、PG 持久化或回撤更新失败。所有交易所调用均为 GET；
公共端口继续使用严格路径、参数、超时和 PG 配额白名单。

## 尚未完成

历史端口已有 [V2 原生不可变结果与 14 日滚动统计](V2_DIRECTIONAL_OUTCOMES.md)，
真实已收盘公共 K 线 T60 调度阶段也已装入统一 daemon pipeline。尚需在
可部署进程入口组装 S6/S8 scheduler 时，将分别绑定的 history/provider 接到
两个 `DirectionalContext` 调度器，并做真实 Testnet 长时间验收。本轮只做隔离
PostgreSQL 和 HTTP 替身 QA，没有访问 Binance。
