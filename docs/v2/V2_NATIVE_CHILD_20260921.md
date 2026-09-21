# Testnet 原生保护单触发接管与结算

2026-09-21，限定独占测试账户 `v2-testnet-primary`，不连接 LIVE。
本次是真实 Binance Testnet 协议验收，不是盈利策略，也不是常驻交易 daemon。

## 已通过的实际闭环

第三个有限尝试 `testnet-native-trigger-20260921-3`：

- episode：`b9802603-705f-58ed-9f5c-071d8342016c`
- BUY 0.0007 BTC，开仓 order `28593851274` / trade `539659141`，成交价 81377.5。
- TAKE_PROFIT_MARKET 父 algo `1000000212559505`，MARK_PRICE 触发价 81385.00。
- 约 5 秒后自然触发，父单 FINISHED，原生 MARKET 子单 `28593851329` FILLED。
- SELL 成交 trade `539659155`，成交价 81376.8，原生子单归属原 episode。
- V2 没有再 POST 一笔平仓单；提交前登记、父子绑定、成交与费用都保存在 PG。
- 最终零持仓、零普通单、零条件单；账本 SETTLED、预算 RELEASED。
- 结算重跑 ALREADY_SETTLED；协议重跑仍通过，未新增订单或成交。

原生子单的 venue client ID 是父单 `v2p-2699d956995a8b13350a6617e2bd`，
不同于本地订单 ID。两者不可混用；普通恢复查询根据不可变绑定查交易所身份。
`ProtectionChildReconciler` 只使用 GET，采用事务内归属锁、数量预留、
PREPARED→SUBMITTING→成交应用，避免暴露可被 worker 二次提交的 PREPARED 子单。
Binance 提交适配器也显式拒绝 `BINANCE_ALGO_CHILD` 来源的提交。

## 为什么止盈触发仍亏损

触发依据是 MARK_PRICE，不是保证成交价。此处是极近阈值的协议测试；
实际平仓成交价低于开仓价，而且两侧手续费远大于这次价差。
不能把条件单名称当作盈利证明，更不能据此评价完整策略收益。

| 账项 | USDT |
| --- | ---: |
| 原始成交价计算毛盈亏 | -0.00049000 |
| 交易所成交回报及 income 已实现盈亏 | -0.00048999 |
| 两侧手续费 | 0.04557120 |
| 显式 CORRECTION 精度差额 | +0.00000001 |
| 最终净盈亏 / 钱包变化 | -0.04606119 |

钱包由 3519.72379300 变为 3519.67773181。
结算 income run：`0479f6b6-5f7b-4528-b5ee-4a92850d69cc`。
结算最终清点：`05d58476-b165-4285-b6eb-370ce74560c8`。
重跑清点：`94a16095-ef26-4389-ae0e-51b752056952`。

没有抹平或覆盖原成交。有限协议结算器仅在逐笔成交回报、收入流水、钱包变化
三方精确一致、一个关闭成交、差额绝对值不超过 0.00000001 USDT 时，
生成保留基准净值、交易所净值、trade IDs、income 摘要和前后清点 ID 的调整。
调整与最终结算在同一 REPEATABLE READ 事务中完成；失败全部回滚。
缺 income、外部流水、资金费、较大差额、任意已有现金调整均不能自动绕过。
这不是适用于所有资产/多仓/所有交易所的通用舍入规则。

## 未通过的尝试也保留记录

1. episode `8cb12544-7e57-56c6-b0a4-f757fbb77d66`：保护单 ACK 后立即查询
   返回 -2013，执行自有仓安全退出，最终 EXPIRED。净值 -0.04739268，已结算。
   修复为同身份 GET 的有限重试，绝不重复 POST。功能验收仍记失败。
2. episode `fead40c1-246c-5edb-bf5b-698eb3371943`：STOP 在 120 秒内未触发，
   自有仓安全退出，最终 EXPIRED。净值 +0.01270957，已结算。功能验收仍记超时。

`ROUNDTRIP_CLOSED` 仅证明资金闭环，可以对账结算；不等于
`PROTECTED_ROUNDTRIP_PASSED`。STOP 自然触发尚未实际观察到。

## QA 与边界

覆盖：原生父子身份错误拒绝、子单跨 episode 归属隔离、禁止重复提交、
部分成交恢复、缺成交保持 UNKNOWN、部分成交后取消只退出残量、
STOP/TP 模拟自然触发及重跑、保护 ACK 后短暂不可见。
资金专项 21 项通过：零/正/负精度差额、证据不一致拒绝、回滚、重跑。
最终全量：**3841 passed / 10 skipped / 1 历史 warning**，240.63 秒。
命令：`env V2_QA_PYTHON=/tmp/v2-qa-python.0ATk4H/bin/python3 bash scripts/qa_v2_data_core.sh -q`。
隔离诊断目录 `/tmp/v2-data-qa.fE4MTG`；Ruff 检查、格式检查和 diff whitespace 检查通过。
跳过项不算通过；历史 warning 是 PM golden 测试返回 bool，而非断言。

尚未完成：通用常驻 PM、原生触发与人工退出全竞争协调、真实部分成交/进程崩溃
恢复演练、多策略与资金费全账户自动结算、真实策略调度和完整发布部署。
本节点不能宣称整套 V2 已完成或可切实盘。
