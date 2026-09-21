# 用户授权的测试账户及旧运行状态清理

## 结论

用户要求全部平仓、清空无用 Redis/本地历史状态，并确认 V2 全流程完成度。
**旧运行状态已清理；币安平仓未完成，V2 整体也尚未完成。**
不能把清缓存等同于交易所平仓，或把单笔协议验收等同于真实策略全自动闭环。

## 币安测试账户（未成功平仓）

全账户清点 `2a16d26b-b33b-4e9e-a475-4b0688f49fd0`：
唯一非零仓位 ZORAUSDT / BOTH / 多 26399，普通挂单 0，条件单 0。
仅使用 BINANCE_TESTNET 凭据及 demo-fapi，未访问 LIVE 交易接口。

复用已测试的 PG CAS 维护执行器，独立 campaign `approved-full-reset-20260921`，
限定已清点的 ZORAUSDT SELL、MARKET、reduceOnly=true、quantity=26399。
原意图及原始仓位指纹先入 PG；首次未取得可核验回执，状态 UNKNOWN，
后续原 clientOrderId 查询 -2013。没有重发该原请求。

在重新确认同一残余持仓、没有挂单、单向模式、交易规则数量合法后，
记录了单独的用户授权残量关闭动作 `approved-full-reset-residual-20260921`，
仍严格 reduceOnly，保留关联的原请求身份；没有删除原 UNKNOWN 证据。
本次捕获交易所明确 HTTP 400 / **-2022（ReduceOnly Order is rejected）**。
未去掉 reduceOnly，未用可能反向开仓的普通 SELL 规避拒绝，也未无限重试。

只读复核证据 `approved-full-reset-20260921:rejection-investigation` 存 PG：

- account 与 positionRisk 都返回 ZORAUSDT 26399，BOTH。
- updateTime 1788914971097；entryPrice 0.00865。
- 普通/条件挂单均为 0，dualSidePosition=false；近期 userTrades 返回空列表。
- 交易所 exchangeInfo 显示 TRADING，步长 1，市价最大量 2000000。

由此不能认定仓位已消失或识别具体拒绝根因。需要币安 Demo 页面/账户端核实并
处理这笔无法通过只减仓 API 平掉的仓位；或提供隔离的干净测试账户后继续交易验收。
本地删除数据不能解决交易所返回的不一致；目前账户仍 BLOCKED。

最后只读覆盖核验 `a4d8e6a7-dc6d-4186-9e90-cb41369f7c8d` 仍报
VENUE_LEDGER_POSITION_MISMATCH。原 4 笔 V2 协议单及其结算记录不变。

## Redis 与本地状态（已完成，可恢复）

清理前核实：旧六个交易服务全部 inactive，旧 Redis 只有维护连接，
DB0 共 487 个键，前缀均归属旧交易项目；V2 使用另一个独立 Unix socket 实例。

1. 备份旧 Redis 为 `/var/backups/trade-engine/legacy-reset-20260921/redis-before.rdb`。
   redis-check-rdb 校验通过，487 个键、checksum OK；SHA256
   `55d61bd456708f1aceabdde709f32a577c6cdaf8889853f0711aecbbb4ac9598`。
2. 将 19 个旧 JSON 状态文件移出原读取路径，按相对路径存入同目录 `files/`。
   包括 S6/S8/sandbox/grid/PM 相关状态、冷却、S3 缓存与信号、旧 checkpoint 等。
   移动前后逐文件 SHA256 一致；没有删除凭据、策略配置、代码或日志。
3. WATCH 已确认的键集合后显式 UNLINK 487 个键，不使用 FLUSHALL。
   旧 DB0 剩余 0，并 SAVE 空状态，防止 Redis 重启恢复旧快照。
4. 清理计划、完整文件清单/大小/SHA256、Redis 键列表及完成结果写入 PG：
   `testnet-maintenance-v1 / legacy-storage-cleanup-20260921`，状态 COMPLETED。

备份约 73 MiB，目录权限 0700，位于业务读取路径之外。可由管理员恢复，但不要
将旧仓位文件直接放回运行路径。V2 PG 审计账本、ClickHouse、配置凭据均保留。
V2 Redis 的两个实时行情键 `v2:market:SANDBOX:s3:BTCUSDT/ETHUSDT` 保留；
它们不是旧持仓，当前数据服务仍在使用。

旧 trade-s0/s3/s6/s8/sentiment/tv 的开机启动均已 disable，服务继续 inactive，
避免机器重启后旧交易程序重建缓存或重新交易；unit 文件和代码未删除，可显式 enable 恢复。
V2 market-pilot 与只读 protection-worker 保持运行，未启动自动策略交易。

## V2 流程完成度

| 环节 | 已验证 | 尚未完成 |
| --- | --- | --- |
| 采集与过滤 | BTC/ETH 公共行情→ClickHouse→S3→PG/Redis | 所有真实策略的生产输入与调度接线 |
| 开仓/保护/平仓 | 固定小额协议单、真实 TP 自然触发、原生子单接管 | 完整常驻 PM、保护创建/撤换、退出竞争、多策略自动开仓 |
| 落库与结算 | 订单/成交/费用追溯，单笔协议三方证据结算 | 全账户资金费、外部操作、强平、迟到数据自动归因与重审 |
| 分析 | 可按成交价差、手续费和显式精度调整解释账务盈亏 | 真实策略完整收益归因与长期表现验收 |
| 部署 | 独立测试库与只读巡检运行 | 持久发布、重启/灾备及完整切换演练 |

最近代码全量 QA 为 3882 passed / 10 skipped / 1 历史 warning；本轮为维护操作及
记录更新，没有据此宣称全部升级完成。Polymarket 已排除，TradingView 后置。
