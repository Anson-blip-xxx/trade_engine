# 2026-09-21 测试网保护单协议接线与实测

用户授权在现有隔离测试环境验证四项交易闭环门禁。本次开始实际协议探测；
**不是完整交易闭环验收通过，也没有启用策略自动开仓。**

## 代码边界

- `v2_core/protection.py`：SANDBOX / Binance USD-M / one-way close-all 保护协议日志。
  PG BusinessState CAS 在 POST 前记录 SENDING；固定 clientAlgoId；丢响应、崩溃、
  重复调用只查原身份，不重发；同 episode/symbol/kind 不允许暗中改变触发价。
- `v2_core/transport.py`：独立且默认关闭的 `enable_testnet_protection`；LIVE 构造拒绝。
  该能力只允许 STOP_MARKET / TAKE_PROFIT_MARKET、closePosition=true，
  不接受 quantity、reduceOnly、开仓 MARKET 或其他写接口。
- 查询核对账户绑定、客户 ID、交易所 ID、币种、方向、订单类型、触发价和 close-all。
  TRIGGERED / FINISHED **不是成交入账证明**，后续必须追踪 actualOrderId 与真实 fills。
- `cancel_flat_once` 只给空仓协议清理使用；有仓拒绝撤单，撤单前同样 PG CAS。
  不是保护单替换方案，不能拿它撤掉持仓的唯一保护。
- `services/v2_testnet_protection_probe.py`：一次性固定测试案例、固定隔离 PG 与 TESTNET key，
  检查旧六服务均 inactive、账户空仓且没有其他订单。没有普通订单开仓能力。
  不是 daemon，不导入旧交易执行器。测试用硬编码触发价不得作为实际策略价格。

## 实际结果（未通过正向创建验收）

1. 前置账户清点 `9791f437-97d2-4513-9579-8286e64ac639`：0 持仓 / 0 普通单 / 0 条件单。
2. STOP_MARKET / BTCUSDT / SELL / 10000 的提交未得到已确认订单；原身份查询 -2013。
   第一版诊断没有保存提交错误码，不能反推具体原因；PG 保留 UNKNOWN，禁止自动重发。
3. TAKE_PROFIT_MARKET / 1000000：提交 HTTP 400 / -4007；查询 -2013。
   只读 exchangeInfo 证实 BTCUSDT PRICE_FILTER maxPrice=809484、minPrice=261.10、tickSize=0.10。
   这是测试参数超过上限，不是权限不足。新增代码保存脱敏错误 class/code/http_status；
   对 -4007 明确拒绝的新增请求直接记录 REJECTED。历史 UNKNOWN 不覆盖抹除。
4. 已明确拒绝的 TP 案例修正为新身份、90000（并非重试未知订单），提交 HTTP 400 / -4509，
   查询 -2013。当时 markPrice=81245.70000000，账户仍空仓。
   当前公开 USD-M 错误表未列出 -4509，**不能仅凭代码把原因断言为余额或权限问题**。
   该场景表明空仓 close-all 探测没有成功，不能据此证明有仓保护可用。
5. 最终清点 `fdaf0c4e-bfea-4bee-af19-f8c5f2da58f1`，state
   `ba99ad53-9718-5b82-a60f-2744769dc307`：0/0/0，无 blocker。
   本次没有任何开仓成交，没有撤销既有保护，没有接入 LIVE。

日志在独立 PG `trade_v2.v2_business_state` / `v2_state_history`，namespace
`testnet-protection-v1`。原始提交成功响应和确认查询存入审计；密钥和签名不落库。
“当前账户空仓”不等于可以把未决身份标记成交或自动重复提交。

## QA 与下一依赖

新增回归覆盖重复调用、并发单次写、PG 故障零写、响应丢失、预提交崩溃、查无订单不重下、
改价身份冲突、持仓禁止撤单、错误身份/价格/布尔字段/状态拒绝、默认禁写及 LIVE 禁止。
首次隔离测试发现 canonical 只接受对象、而状态 key 传了数组，已改为对象后重跑。
首次修正后保护+传输定向 QA 42 passed；最终全量 **3793 passed / 10 skipped / 1 历史 warning**，
203.53 秒，诊断 `/tmp/v2-data-qa.mSCcfm`；新增 16 个用例。Ruff 与 git diff --check 通过。

下一步仍需完成：交易所过滤器驱动的价格与数量、受控单笔开仓及失败时 reduce-only 退出、
持仓条件下保护创建/触发/成交核验、持久 PM、自动对账及结算。不能用这次负向协议探测
关闭策略、保护、结算四项真实验收门禁，也不能把手工协议单计入策略绩效。

官方协议参考：
[USD-M Trade API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
及 [USD-M 错误码](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code)。
