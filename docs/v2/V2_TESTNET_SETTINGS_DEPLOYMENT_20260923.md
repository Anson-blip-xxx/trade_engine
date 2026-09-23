# V2 Testnet 设置协调与分阶段写入验收（2026-09-23）

## 发布物与 QA

远端 `feature/v2-architecture` 提交
`15f8a82f30c4686a852b37672d37b3108180a96f` 构建为 root 所有、不可写 release：

`/opt/trade-engine-v2/releases/15f8a82f30c4686a852b37672d37b3108180a96f`

锁定运行时保持 `/opt/trade-engine-v2/venvs/py312-locked-20260923-v1`。改动文件 Ruff
通过；设置/readiness/transport/inventory 定向 QA 为 125 passed，V2 核心为
1265 passed，全仓为 4218 passed、10 skipped、1 条既有 PM golden warning。

## 分阶段开启结果

部署后先保持三项写权限全关。多个周期为 `CYCLE_COMPLETE`，daemon/watchdog 均
`active/running`、`NRestarts=0`，订单表无更新。随后依次验证：

1. 仅开启保护写入和 reduce-only 退出，仍禁止开仓；多个周期通过，无交易所订单变化；
2. 开启 SANDBOX 账户绑定的 entry/protection/exit 三项权限；现有 8 条历史 FILLED
   订单无更新，且没有不合格信号触发 symbol settings 或下单；
3. Demo `exchangeInfo` 只读核验后，把采集范围由 BTC/ETH 扩展为 10 个可交易 USDT
   永续标的。账户仍只允许一个 V2 管理仓位，扩标不放大并发持仓上限。

扩标后真实采集到 ADA/XRP/DOGE 的 `TREND_DOWN`。信号经过 S3 持久化、S0/Redis
上下文、签名账户上下文、S8 不可变决策和调度追平；强度 21–28，低于冻结阈值 60，
所以结果为 `strength_below_minimum / IGNORED`。这是完整过滤链的真实证据，不为制造
成交而降低阈值。上下文暂时不可用时周期为 `ENTRY_BLOCKED`，任务退避/原始期限到期后
自动收敛，后续连续恢复 `CYCLE_COMPLETE`。

本观察窗没有自然合格 OPEN，因此尚未产生 `venue-symbol-settings-v1`，也没有验证新
协调器对真实交易所的 POST。不能把“权限已开”描述成自然开仓—保护—退出—结算已经
完成；daemon 将继续在 Testnet 等待合格信号。

## 回滚和依赖故障演练

全部写权限关闭后完成实际回滚：daemon 从新 release 回滚到
`a29565a40219ca16e11fd2b41976dc259059d8ce`，产生只读 `CYCLE_COMPLETE`；随后前滚到
`15f8a82...`，再次 `CYCLE_COMPLETE`，两个方向均 `NRestarts=0`。

同样在全部写权限关闭时停止 `trade-v2-clickhouse.service`。systemd `Requires=` 使
交易 daemon 停止，独立 watchdog 保持 active；重新启动 daemon 后 ClickHouse 被依赖
链拉起，preflight 通过，三服务均为 active、0 重启，流水恢复 `CYCLE_COMPLETE`。
这验证了失败关闭、独立监督存活和依赖恢复路径。watchdog 的 TG 是 best-effort 且不
落本地状态，本次只能证明故障/恢复探测窗口已覆盖，不能从本机数据库证明 TG 到达。

## 当前状态与剩余边界

最终 daemon 使用新 release，10 标的，SANDBOX 账户确认绑定，entry/protection/exit
均为 true；LIVE 没有启用。独立 watchdog 保持 active。整机 reboot 尚未执行，自然
合格信号的真实设置—开仓—保护—退出—结算闭环也仍待市场触发；这两项不能用固定
协议单或模拟 QA 冒充。

部署后审计又发现最终结算没有把同一外部持仓排除传入 final coverage。提交
`b658d00f074744285c7132d163e0a59de8db2d67` 已修复，并让 coverage inventory 本身保存
排除列表/排除持仓，而不只在结果比较时临时忽略。相关定向 97 passed、完整 V2 核心
1267 passed；对应不可变 release 已部署，首个真实周期 `CYCLE_COMPLETE`，三项 Testnet
权限保持 true、daemon/watchdog 均 0 重启，订单和 symbol settings 仍无新增。
