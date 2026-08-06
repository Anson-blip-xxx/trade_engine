# 后续优化清单

记录于 2026-08-06，暂不直接改变实盘开仓行为。

- 接入 Binance 订单簿深度、买卖墙和盘口不平衡。
- 用 PostgreSQL 中的 taker flow、订单簿和成交结果做回测。
- 将 taker buy ratio / orderflow bias 用作开仓过滤或仓位调整条件。
- 将分析查询逐步从 ClickHouse 切换到 PostgreSQL 主账本。
- 配置 Binance 成交对账脚本为每日定时任务。
- 评估 Post-Only 限价入场，超时后再转市价，先回测成交率和滑点。
- 补齐订单级、成交级和持仓级的完整链路分析。
