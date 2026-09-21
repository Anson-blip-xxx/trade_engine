# 已授权的旧测试账户清理

2026-09-21 用户明确同意平掉UB多仓、SAGA空仓并清理100个遗留条件单。
仅使用指定TESTNET凭据与固定demo-fapi.binance.com，不涉及实盘，不删除历史数据。
旧6个交易服务停止；V2数据侧继续运行，没有启用策略新开仓。

## 实际结果

| 动作 | 数量 | 交易所证据 |
| --- | ---: | --- |
| UBUSDT只减仓市价卖出 | 1677 | orderId=481131293，FILLED |
| SAGAUSDT只减仓市价买入 | 5626 | orderId=343757583，FILLED |
| 按原algoId逐个撤销条件单 | 100 | 每个均查询确认CANCELED |

两笔平仓均为BOTH单向、reduceOnly=true；按原clientOrderId查询订单，并核对
全部成交数量及手续费/已实现盈亏字段，保存完整成交回执。它们是旧系统退出时
的人工授权维护动作，**不是V2策略新交易，也不能拿来证明V2策略盈利能力**。
平仓费用证据不等于旧仓整个生命周期的完整费用归因。

最终独立清点：0持仓、0普通挂单、0条件单，无阻断项。
observation_id=`1ecb154d-1a40-41ec-90ae-c987c6502d12`，
state_id=`05062c3e-1ec8-5f18-ad30-6ba4ae3a1b69`。
TG仅投递该最新完成事件的环境、状态、事件ID和0/0/0计数，回执成功。
旧历史告警未删除，也未在完成通知时重放。

## 审计与重试边界

`services/v2_testnet_cleanup.py` 限定本次固定campaign、账户和两个获批方向；
campaign=`approved-legacy-cleanup-20260921`。执行前检查旧服务状态、保存账户
快照与计划，并核对旧仓数量/方向/入场价/updateTime，变化则停下。

计划和每个动作写入PG BusinessState及不可变history，namespace为
`testnet-maintenance-v1`。发送前CAS为SENDING；收到回执后ACK_RECEIVED；
查询核实后CONFIRMED。此次2个CLOSE、100个CANCEL均CONFIRMED。
PG失败不得发送；SENDING/UNKNOWN重启后只查询，不盲目重发。原计划已固定，
再次运行不创建新计划或重复执行已确认动作。

首次撤单发生短暂的回执/查询可见性差异，工具安全停止。核实后增加最多5次、
间隔0.5秒的只读状态重查；不重试DELETE。恢复执行未重复平仓或撤已确认单。
清理前后完整核对普通挂单；每次撤销旧条件单前检查账户无持仓。
请求有PG持久权重预算，且不使用按品种批量删除来扩大撤单范围。

传输层新增仅测试网可显式开启的单条件单撤销能力，默认关闭；LIVE不能开启该
维护开关，也不开放allOpenOrders/algoOpenOrders批量撤销接口。
官方契约：[撤销条件单](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#cancel-algo-order-trade)。

## 下一阶段

本次新增15项回归，定向41项通过；最终全仓3777 passed、10 skipped、1个既有warning，
200.48秒。Ruff/格式/diff通过；临时QA服务停止，诊断 `/tmp/v2-data-qa.6tWLFD`。

空账户基线已建立，旧仓移交阻断解除；还须继续接通真实策略下单、账户原子风控、
保护单、正常平仓、自动对账/结算与分析。账户清空不是交易许可，完整V2尚未交付。
常驻数据侧保持无交易端口，不能把维护脚本改作日常策略执行器。
