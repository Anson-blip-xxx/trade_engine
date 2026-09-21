# 部分开仓撤余单与残仓保护

本节点是默认禁写、测试网限定的恢复组件，不是已部署的自动 PM，也不关闭 V2 发布门禁。
用户已明确搁置 ZORA；本节点不请求其行情、不平仓、不忽略账户异常、不新增测试账户交易。

## 顺序与持久性

`PartialOpenProtection.ensure(order_id, stop_spec)` 校验原订单所属账户、episode、symbol、
开仓方向及 STOP 类型，然后依次执行：

1. 在 PG 写入不可自动解除的 `episode-opening-halt-v1`。与订单准备、提交使用同一
   intent 根行锁；同 episode 不能新增开仓单，已 PREPARED 的单也不能提交。
   已发送的单仍必须逐单恢复/撤销。平仓和同身份重放不被禁止。
2. `TestnetOpeningCancel` 查询原订单和逐笔成交。已成交/失效则直接采用终态，
   PREPARED 可在 PG 内取消，不发送交易所请求。
3. 非终态先在 PG 原子登记 `opening-cancel-v1 / REQUESTED`，提交成功后最多发一次
   精确 `symbol + origClientOrderId` 的 DELETE。禁止批量撤单和外部订单身份。
4. 不把 DELETE 返回当成成交证据。原身份 GET order + userTrades 进入现有账本，
   只有逐笔数量完整才确认 CANCELLED/FILLED。超时、丢响应、登记后崩溃均只查询恢复。
5. 未确认终态返回 `OPENING_NOT_FINAL`，不宣称已保护；确认无账本残仓返回
   `NO_LEDGER_EXPOSURE`；有残仓调用 `GuardedStopInstaller`，重新做全账户/行情/PG
   一致性检查，仍默认禁创建。

默认 `allow_writes=False` 不写 halt、不撤单、不创建保护。已有撤单登记仍可只读恢复
并补齐 PG 成交账本。传输另需显式 `enable_testnet_order_cancellation=True`，
独立于下单、条件单撤销和保护创建开关；不能用于 LIVE。运行服务未开启这些写权限。

## 并发与故障边界

- halt 与开仓提交争用同一根行锁：halt 先提交，后续开仓被拒绝；提交先完成的订单
  可能已到交易所，不能声称 halt 能撤回该请求。
- 账户 advisory lock 串行化本组件、已有协议/保护组件；尚未覆盖所有未来执行路径。
- 登记提交前数据库失败：无 DELETE；登记之后失败：只查询，不因未查到而重新撤单。
- 取消超时且余单仍活跃：显式阻断，不能称为无裸仓窗口。仍需未来监督调度及限时
  升级处置；不通过“先止损、让开仓余单继续成交”掩盖风险。
- 如果同 episode 有其他活跃开仓单，或账户有外部仓位、未知订单等，保护安装仍被阻断。
- 不撤换已有保护，不自动补发开仓，不释放未核实的预算，不使用本地文件保存业务状态。

## QA 范围

隔离 PostgreSQL + 注入 Binance 协议：部分成交→撤余单→确认成交/费用→STOP，
超时已接受/未接受、缺失成交明细、终态抢先、迟到成交、登记失败/登记后崩溃、
错误归属、账户互斥、默认禁写、独立传输开关、halt 阻止补单和 PREPARED 提交。
另运行全仓回归。真实交易所部分成交恢复和自动 PM 常驻验收尚未完成。

最终代码全仓回归：**3934 passed / 10 skipped / 1 warning**，291.30 秒；
诊断目录 `/tmp/v2-data-qa.MMPIX1`。告警来自既有 PM golden 测试返回 bool，非新增失败。
本节点新增 31 个测试用例；包含并发撤单、冻结与补单竞争，以及 UUID 大小写不能
绕过撤单身份/开仓冻结。新增及修改 Python 文件 Ruff 检查通过。
运行中的五个 V2 服务只读检查均为 active；未重启、未开启新的交易所写权限。
