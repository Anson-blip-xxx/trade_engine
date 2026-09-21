# 2026-09-21 有仓保护单真实测试网验收：通过

后续进展：[本单已经证据驱动正式结算 SETTLED](V2_PROTOCOL_SETTLEMENT_20260921.md)。
以下账务 CALCULATED 描述保留为当时验收记录。

用户要求继续调试至通过。本次限定为单笔协议闭环验收，不是所有真实策略已迁移，
也不是自动 PM、条件单触发成交、全账户自动结算全部完成。

## 实际链路

`services/v2_testnet_roundtrip.py` 使用独立 PG、原 TESTNET key、demo-fapi 域名。
交易所 exchangeInfo 与最新 mark → Decimal 数量/价格过滤 → PG 不可变计划与决策 →
账户空仓/余额复核 → PG 原子预算（100 USDT、1 仓位）→ V2 开单 → 两张保护单 NEW →
V2 reduce-only 平仓 → 订单/成交/手续费入账 → 保护单终态查询 → 全账户 0/0/0 → PG 验收记录。

- 固定单次 campaign `testnet-protected-roundtrip-20260921-v1`，不循环开仓。
- episode：`6545a023-8fe1-59c7-9d45-d39d49652999`。
- PG 会话 advisory lock 防止多个 CLI 同时运行；意图/订单身份固定，不复活过期计划。
- 开仓参考名义约 56.89 USDT，0.0007 BTC。不是固定数量绕过交易所过滤器。
- 保护测试失败也进入 finally 中已登记的平仓流程；未知开仓先查询，不能直接再开一笔。
- 关闭完成后重跑只核验原订单与历史保护确认，不再开仓；失败保护历史不能冒充通过。
- 仍有边界：进程被强杀/主机宕机需要重启本命令恢复，非完整常驻 watchdog；
  外部操作或保护单先触发需专门对账，不能将本单受控测试程序视为完整 PM。

## 交易所证明

| 项目 | ID / 实际结果 |
| --- | --- |
| 开仓 | order `28593819038`，trade `539649143`，BUY 0.0007 @ 81266.9，FILLED |
| 止损 | algo `1000000212531376`，client `v2p-d3ca79ba90949a0a3e4317bea135`，确认 NEW |
| 止盈 | algo `1000000212531382`，client `v2p-39417d751d7bf5428309026621a2`，确认 NEW |
| 平仓 | order `28593819048`，trade `539649146`，reduce-only SELL 0.0007 @ 81215.2，FILLED |
| 保护清理 | 平仓后两张 close-all 单被交易所置为 EXPIRED；原身份查询确认，不以空列表推断 |
| 最终账户 | observation `0ce547b1-26ea-4bca-ac56-05e1cd62682a`，0 持仓/0 普通单/0 条件单，无 blocker |
| 验收输出 | `PROTECTED_ROUNDTRIP_PASSED`，恢复重跑仍只有 2 个普通订单、2 笔成交 |

## 本次定位与修复

先前空仓探测没有证明有仓保护可用；此次真实有仓下两种保护均创建成功。
这支持改变测试前置条件，而不是猜测错误码含义；不把未公开的 -4509 文案当作已确认事实。

第一次真实平仓后，GET 与 DELETE 之间交易所自动终结 close-all，撤单返回 -2011。
修复为 **只查询相同身份**：确认 EXPIRED/CANCELED 才视为收尾完成，仍 NEW 则失败/查询等待。
没有批量撤单、重复 DELETE 或把任意错误当成功。

## 账务三方核对

- 成交价差：`-0.03619 USDT`。
- 开仓手续费：`0.02275473 USDT`；平仓手续费：`0.02274025 USDT`。
- 账本净结果：`-0.08168498 USDT`。
- Binance income 导入 run `0f7edf9f-4496-4586-bd1d-7bca51cd6dc5`，FETCHED，1 页 3 行：
  2 笔 COMMISSION、1 笔 REALIZED_PNL，tradeId 与上述成交匹配，金额一致。
- 前置 inventory `4a8b4cb4-d1aa-4b20-b05b-4f8a9c7d4476` 到最终 inventory 的
  totalWalletBalance 差额也为 `-0.08168498 USDT`。
- 当前账本状态仍为 **CALCULATED**，没有用一次 FETCHED 或手填布尔值冒充
  通用自动现金结算完成；自动资金费归属、迟到流水和持续结算仍是后续门禁。

这笔损失可追溯到入场/出场价差和两笔手续费，是短时协议验证成本，不是策略收益样本。

## QA

保护协议和真实 PG roundtrip 定向测试 **26 passed**，包括保护失败仍平仓、开仓响应丢失恢复、
重复执行零新订单、已有仓位拒绝、价格/时间校验，以及撤单拒绝后必须有终态查询证据。
全量回归 **3803 passed / 10 skipped / 1 历史 warning**，207.44 秒，诊断
`/tmp/v2-data-qa.1MGU0Z`；Ruff 与 git diff --check 通过。
旧运行仓库未修改，未使用 LIVE 凭据。
