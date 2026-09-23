# V2 全局市场状态（S0）

## 已完成的数据链

`services/v2_s0_regime.py` 将 S0 接到统一 Testnet daemon，权威链路为：

`Binance 已收盘 1m K 线 → ClickHouse 内容寻址归档 → PostgreSQL S3 确认收据
→ 同一原始批次重算 S3 窗口 → S0 分类 → PostgreSQL S0 批次 → Redis 当前投影`。

S0 不读取旧本地状态文件，也不把 Redis 当历史事实源。Redis 丢失时可用相同归档和
PG 收据重放；PG 记录成功但 Redis 投影失败时返回 RETRY，策略准入保持关闭。
S0 frame 身份包含算法版本和原始收盘时间，同身份不同内容由 producer 冲突检查拒绝。

## 严格输入和分类

每轮只采用 PG 中最新一条 delivery。它必须处于 `ACKNOWLEDGED`、存在不可变确认事件，
并且 S3 frame 摘要与 delivery 摘要一致。归档的内容摘要、重算 frame 身份、观察时间和
输入摘要必须再次匹配；读取期间出现更新也拒绝本轮结果。

配置的 symbol 集合必须完整且包含 BTCUSDT。所有标的必须来自同一收盘边界，拥有完整
15m/1h/4h/24h 窗口和 S3 输入证据。缺币、额外币、混合时间、非有限数值、零价格、
摘要错误或归档不可用都不会回退为 neutral。

分类保留原 S0 的关键价格规则：BTC 4h EMA 趋势、15m 振幅与 15m/24h 波动扩张、
以及配置 universe 的 1h `close > ema20` breadth；risk-off 的严格阈值语义不变。
旧版情绪、alts-sync 和 shock 外部源尚未迁移，输出明确标为 `NOT_CONFIGURED`，不会
伪造 50 或 0 等中性观测。S6/S8 当前只消费经过证据绑定的 regime。

## 统一流水门禁

S0 位于行情阶段之后、S6/S8 调度之前。只有行情返回 ACKNOWLEDGED/CURRENT，且 S0
成功写 PG 并投影 Redis，本轮才可继续准入。行情失败时仍先执行恢复、保护、退出、
结算和 T60，但 S0 标记 `MARKET_NOT_CURRENT`，不产生新决策或新开仓。

## QA 与未完成项

隔离 QA 覆盖 bull-trend、0.04 振幅严格边界、risk-off、缺失/额外 universe、混合时间、
非有限数值、不修改输入，以及模拟公共 HTTP → 归档 → S3 确认 → S0 PG/Redis →
实际 S6/S8 调度。进程工厂测试确认构造时无网络 I/O，S0 是强制依赖。

这些测试使用一次性 PG/Redis 和 ClickHouse/HTTP 替身。本节点没有启动 systemd、访问
真实 Binance、读取密钥或发送订单。仍需隔离 Testnet 实例的真实依赖长跑、重启、
Redis/CH/PG 中断恢复、universe 容量与阈值回放，以及自然策略信号全生命周期验收。

最终全仓回归为 4165 passed、10 skipped、1 条既有 PM golden 返回值 warning；
变更文件通过 Ruff、format 和 diff 检查。
