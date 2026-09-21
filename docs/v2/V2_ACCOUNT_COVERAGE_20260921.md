# 账户持仓、挂单与保护覆盖核验

## 新增能力

`AccountCoverageAudit` 只读 Binance Testnet：将前后 PG 一致性快照与交易所账户清点
对应，核查逐 episode 净成交、实际持仓、普通单及登记的 STOP_MARKET 保护。
仅 `SANDBOX/BINANCE/FUTURES`，单向、单资产、同品种独占模型。

- PG 净持仓由精确成交数量计算，不信任缓存汇总；空/多方向分别带符号。
- 交易所读取前后核对持仓；PG 读取前后核对意图修订、订单版本、保护版本与事实。
- 同品种多 episode、持仓不一致、过量平仓、非终态本地订单、未知普通/条件单均阻断。
- STOP 必须同时存在于本地登记和当前交易所挂单，完整核对身份、方向、MARK_PRICE、
  closePosition、价格、状态等；只存在登记或未登记条件单不算有保护。
- 所有普通挂单都要求进一步恢复核验，即使其身份已知也不返回 CLEAR。
- 原始账户响应、前后账本事实、分类结果保存在 PG 不可变 history，最新结果与
  异常 outbox 同事务；任何一步失败全部回滚。不保存本地业务状态。
- 相同异常按五分钟桶合并；BLOCKED→CLEAR 产生恢复通知，持续 CLEAR 保持安静。
- 复用账户 session lock，与协议测试及保护恢复互斥；查询使用同一 PG 配额预算。

每轮结果始终 `execution_authorized=false`。这是诊断，不是完整开仓风控门禁，
也不触发接管、平仓、撤单或自动修复。REST 非原子；即便前后数量一致也不能排除
区间内交易后恢复同数量的变化。不能把此结果用作持久交易许可。

## 接入

保护巡检 CLI 增加 `--audit-account`：每约 60 秒做一次账户核验，其余保持原 15 秒
保护恢复轮询；显式参数启用，未开启时不扩大原只读端点集合。
只读配额采用现有协议脚本的完整权重，按最大 10 的分块申请，不少计全账户查询。
新诊断复用 `PROTECTION_RECOVERY` 类型，本轮无需数据库 schema 改动。

```sh
/tmp/v2-qa-python.0ATk4H/bin/python3 -m services.v2_testnet_protection_worker \
  --config /home/ubuntu/.opencode/trade/trading_engine/config/binance.env \
  --once --notify --audit-account
```

## 真实测试网发现（不是测试通过后可忽略的结果）

第一次真实观测 `836c5735-a249-4033-abdf-ade0502b4269` 返回
`ACCOUNT_COVERAGE_BLOCKED / VENUE_LEDGER_POSITION_MISMATCH`。
账户有 `ZORAUSDT BOTH +26399`，入场价 0.00865；没有普通挂单或条件单。
PG 的 4 笔 V2 协议 episode 全部为零净持仓，订单/成交仍为 8/8。
该仓不属于现有 V2 登记，本轮没有发出任何订单请求。

异常 PG 证据与 TG 告警回执均成功保存，首次 claimed=1/delivered=1/failed=0。
旧 s0/s3/s6/s8/sentiment/tv 服务检查均 inactive，当前 V2 数据和恢复服务没有
交易写权限。本轮没有擅自平仓、添加保护或给这笔仓位伪造 V2 episode。

交易所报告 updateTime=1788914971097（2026-09-09 00:49:31.097 UTC），
但此前清点 `94a16095-ef26-4389-ae0e-51b752056952` 等保存为空仓。
**来源与差异原因尚未确定**：不能凭这两个快照推断新增开仓者或认定某程序失控。
此账户不一致在后续新增交易验收前必须先解决，不能静默忽略。

## QA 和剩余边界

账户覆盖、保护巡检和账户通知专项 72 项通过，后补空头方向用例进入最终全量。
覆盖缺保护、错误身份/参数、账外仓位、未知订单、对冲模式、API 失败、
读取期间本地/远端变化、告警去重及恢复、事务回滚、锁竞争、作用域与配额门禁。
最终全量 **3882 passed / 10 skipped / 1 历史 warning**，262.44 秒；
命令 `env V2_QA_PYTHON=/tmp/v2-qa-python.0ATk4H/bin/python3 bash scripts/qa_v2_data_core.sh -q`，
诊断 `/tmp/v2-data-qa.ZYWDkk`。Ruff、格式及 diff 检查通过，跳过项不算通过。
历史 warning 仍为旧 PM golden 测试返回 bool。

仅重载了 V2 只读 `trade-v2-protection-worker.service`，新增 `--audit-account`，
其余权限隔离设置沿用前一版；实际 active/running、NRestarts=0。
服务首次观测 `d21286ed-5424-4745-ae77-7af05763ac0d` 仍为 BLOCKED，
同一告警时间桶未重复投递（claimed=0），证明既能持续核验也能合并告警。
旧交易服务未启动，账外 ZORA 仓位未被处理。

仍待完成：全账户成交/income 自动归因、外部操作与强平接管、保护自动创建和撤换、
真实策略调度、人工确认后处置外部仓位的授权流程。
当前每类 PG 事实最多 1000 条，超过则报错且不返回本次 CLEAR，不截断后假装完整。
规模化分页/历史归档是后续部署门禁；PG 不可用仅有进程日志，外部监控仍需完善。
最新记录是历史观测而非永久健康保证，消费者必须检查对应观测时间；尚未接入
任何策略开仓授权。当前服务仍使用 `/tmp` 测试工作树，不是正式持久发布布局。
