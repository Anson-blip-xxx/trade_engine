# V2 自然策略全生命周期验收（2026-09-23）

本节点在 Binance Demo/Testnet 完成了非固定协议、非手工造信号的完整链路：
真实公共行情采集 → S3/S0 → S6 正式准入 → PG 原子风控 → 合约设置协调 →
市价开仓 → 原生止损 → 主动退出 → income/钱包对账 → 结算 → 结果归因 → 风险释放。

这不是 LIVE 上线批准，也不承诺策略盈利。实盘凭据未启用，账户级 `max_notional`
仍为 100 USDT，`max_positions` 仍为 1。

## 不可变审计结果

- 自然信号：`TAKEUSDT / VIOLENT_BULLISH / strength=99`，正式消费者 `s6`。
- episode：`6ecb4190-c88e-5f44-bf6c-9c026a6ae555`。
- 开仓：本地订单 `7c0c5f8b-f757-5c83-97be-f15b06a1ae92`，交易所订单
  `243711647`，`BUY 494`，成交价 `0.19752`。
- 风控：policy v2，预留名义价值 `99.82752 USDT`；实际成交名义价值
  `97.57488 USDT`。readiness 快照确认交易所配置为 `2x / CROSSED`，与计划一致。
- 保护：`STOP_MARKET`，触发价 `0.18188`，algo
  `1000000215562696`，远端状态曾确认 `NEW`；平仓后确认 `EXPIRED`，未重复提交。
- 主动退出：`EARLY_LOSS_MOMENTUM_WEAK`，本地订单
  `337df48a-8fb8-5ea4-b5d4-ca25efa839ce`，交易所订单 `243724945`，
  `SELL 494`，成交价 `0.19203`。
- 最终状态：episode `SETTLED`，risk reservation `RELEASED`，结算与 T0 outcome
  各 1 条；净损益 `-2.80827885 USDT`，回报率 `-2.878075637910085055%`，
  质量分 `88`。开平手续费、realized PnL、钱包差额和成交身份均进入 PG 证据。
- 流水线最终回到 `CYCLE_COMPLETE`，保护、退出、结算阶段均 `CLEAR`。

## 真实联调发现并修复的问题

1. 账户上下文先截取结束时间、后读取 PG 历史，可能把刚生成的历史误判为未来数据。
   `55fffc2` 将历史读取纳入同一采集区间。
2. 策略按余额风险计算的数量可能超过账户 PG 名义价值上限。
   `1bbacd4` 将可用 PG 风险预算作为正式 sizing 上界，未放宽 100 USDT policy。
3. 外部持仓排除只到外层覆盖审计，没有进入止损安装器，导致合法策略仓缺保护。
   `25c58b1` 贯穿止损链；失败期间立即关闭新开仓，止损确认后才恢复。
4. 同一排除没有进入主动退出覆盖审计。`e2f5e7a` 贯穿退出链。
5. 真实 1h EMA、收益率和风险倍数会产生超过 18 位的循环小数。
   `6a0713a`、`da69183` 以显式 half-even 规则收敛到 PG `NUMERIC(38,18)`。
6. Binance income 时间按整秒落点，而 userTrades 保留毫秒，精确 fill 窗口漏掉同一
   trade 的佣金。`77f32aa` 仅扩展前后 999ms，仍按 trade ID、金额、币种和时间线核验。
7. outcome 错把计划名义价值要求为实际成交名义价值，市价滑点会阻断归因。
   `f6b1731` 改为数量身份严格相等，同时分别保存 planned/actual notional。

## QA 与发布状态

- 最终完整 V2 核心隔离 QA：`1274 passed in 441.84s`。
- 最后结算链 targeted QA：50 passed；outcome/settlement targeted QA：28 passed。
- Ruff 与 `git diff --check` 通过。
- 自然生命周期验收 release：`f6b17319d7db4ee4ba758719a152acd55d31e673`；
  最终文档与验收固化提交：`a6f509c6b556a546c5a606cbec9f2d9591528335`。
- `trade-v2-testnet-daemon.service` 与 watchdog 均 active。发布后的受控 `SIGKILL`
  故障注入中，两者各自动恢复一次（`NRestarts=1`），恢复后流水线重新达到
  `CYCLE_COMPLETE`，保护、退出和结算均为 `CLEAR`，风险预留为 0。
- Testnet entry/protection/reduce-only 三项权限已恢复；LIVE 仍无授权。

## 仍属后置或人工发布门禁

- TradingView 按用户决定后置；Polymarket 不进入 V2；S7 网格不阻塞当前方向策略发布。
- 尚未执行整机 reboot 演练；systemd 冷启动、进程崩溃自动拉起、依赖 readiness、
  release 前滚/回滚已验证。
- Testnet 验收不能替代 LIVE 凭据、生产限额和切换窗口的单独批准。
