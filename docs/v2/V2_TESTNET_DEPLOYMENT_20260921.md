# V2 测试网接管准备与真实数据联调

后续状态更新：用户已授权并完成旧测试仓/100条件单清理，空账户基线与回执见
[清理执行记录](V2_TESTNET_CLEANUP_20260921.md)。下文保留清理前的联调事实。

## 授权与范围

用户 2026-09-21 授权停止旧交易服务、新建隔离存储、复用 TG 配置，目标为
采集→过滤→下单→平仓→落库→分析。Polymarket 不再纳入本次 V2 交付；
TradingView 后置。以下记录不等于完整交易链已经交付。

本次没有下单、撤单、平仓或删除历史数据。新进程无交易发送端口；现存旧仓
与遗留单处理已单独询问，未在未明确处置方式前自行清空账户。

## 已执行的环境变更

- 已停止 `trade-s0/s3/s6/s8/sentiment/tv.service`，复查均 inactive、MainPID=0。
  未删除 unit 或配置，未停止 SSH、旧 Redis 等共享设施。旧 unit 仍 enabled，
  重启主机前必须处理旧服务自启，不能将本次 stop 视为永久隔离。
- 新 PostgreSQL 16 集群 `tradev2`，端口标识 55432，仅 Unix socket，fsync=on；
  数据库 `trade_v2_testnet`，schema `trade_v2`。应用角色 ubuntu 无超级用户、
  建库/建角色/复制权限，使用本机 peer 认证；public schema 已撤销 PUBLIC 权限。
  旧 `16/main` 保持原来的 down 状态，没有覆盖其数据目录。
- 新 Redis `trade-v2-cache.service`，只监听
  `/var/lib/trade-engine-v2/redis.sock`（0700），128 MB/noeviction，不做文件快照；
  Redis 仍是可重建缓存。旧 Redis 数据未更改。
- 新 ClickHouse `trade-v2-clickhouse.service`，只监听 127.0.0.1:18123/19000，
  独立数据库 `trade_v2_testnet`，使用仓库 V2 归档表。数据与日志分别在
  `/var/lib/trade-engine-v2/clickhouse`、`/var/log/trade-engine-v2`。
- 新 cache/ClickHouse 为 systemd transient unit、非 root ubuntu 运行，
  NoNewPrivileges=yes；配置见 `deploy/v2-testnet/`。当前是联调环境，
  不是完整开机恢复部署。PG start.conf 为 manual。

数据库自己的持久文件是数据库存储，不是应用用 JSON/CSV 文件代替并发账本。
配置文件和诊断日志不作为持仓、订单或幂等事实源。

## 真实账户清点与告警

固定 SANDBOX host，仅选择 `BINANCE_TESTNET_API_KEY/SECRET`，绝不回退 LIVE key。
只读清点包含单/双向模式、联合保证金模式、账户、前后两次持仓、普通单、条件单。
对条件单使用官方 [openAlgoOrders 接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#current-all-algo-open-orders-user_data)。

当时测试账户为单向、单资产模式；有 UBUSDT 多仓及 SAGAUSDT 空仓，普通挂单为0。
全账户条件单接口返回100条，99条在TUSDT；对UB和SAGA分别查询条件单均为空。
这是瞬时清点，不能推断历史全月状态，也不能仅凭TUSDT名字断言这些订单的来源。

新 PG 中两条账户快照及两条运维 outbox 已原子提交，TG 两条回执均成功。
示例 observation_id：`07414248-1ee2-442d-a59c-3d5f7d7eb6b1`。
摘要状态 `BLOCKED`，原因 `EXISTING_POSITION_REQUIRES_RECOVERY` 与
`EXISTING_CONDITIONAL_ORDERS`；V2交易意图和订单均为0。

TG 只发环境、事件ID、状态/原因代码及计数，不外发账户余额、盈亏、币种、
原始交易所响应或凭据。发送前筛选账户/环境与事件种类，再领取租约；失败保留
队列，可重启重试。Telegram 无业务幂等键，发送成功但回执丢失可能重复通知。
当前通知不是人工交易授权入口。

入口：`python -m services.v2_testnet_inventory --config <现有配置路径>
--account-id v2-testnet-primary [--notify]`。固定独立数据库并校验实际 cluster_name，
不会自行初始化数据库。账户为空也只标记 CLEAR_FOR_RECOVERY_CHECKS，绝不当作交易许可。
REST读取非原子快照，前后数量一致也不证明中途未发生往返成交，仍需要持续对账。

## 真实行情联调与已修问题

`services.v2_testnet_market` 是明确限定 BTCUSDT/ETHUSDT 的数据侧 pilot，不是全市场
策略服务。通过既有 create_market_pipeline 接通真实测试网公共行情、CH小时分块、
PG投递账本、S3检测与Redis上下文，不使用交易API凭据，不提供下单端口。

首次真实CH联调发现 FixedString(64) 经 clickhouse-connect 返回bytes，而旧校验
假设str，导致入队时 ArchiveIntegrityError。现明确按ASCII严格解码64位小写hex，
保留完整摘要校验；非ASCII、截断、NUL填充等仍拒绝。补7项回归，未绕过校验。

修复后真实批次 `s3-closed-1m-v1:1789955820000` 已 ACKNOWLEDGED，生成信号
`d46b381f-de6f-5b37-93f4-da3ac5b0e70e`；当时CH有1个manifest、54个唯一小时块
（含第一次失败遗留块），Redis有BTC/ETH两个SANDBOX上下文。
未引用的旧归档块保留，未擅自执行GC。旧错误告警也保留，不删除故障证据。

入口：`python -m services.v2_testnet_market [--serve] [--notify-config <配置路径>]`。
仅采集进程可以常驻，交易执行仍关闭。扩展币种集合须处理源版本与身份，
不能在相同帧ID下悄悄更换覆盖范围。

现已启用 `trade-v2-market-pilot.service`（transient、ubuntu、NoNewPrivileges），
10秒检查一次收盘边界；日志 `/var/log/trade-engine-v2/market-pilot.log`。
运行中又完成批次 `s3-closed-1m-v1:1789956120000`。显式重启该数据侧服务后，
同边界返回 CURRENT，未重复生成该批次；此前归档故障告警已重试送达，
新库运维回执累计3条。此为一次正常进程重启验收，不是整机/PG故障演练。

## QA

本次新增44项回归（账户清点/凭据选择/告警/作用域、FixedString兼容等）。
最终全仓 **3762 passed、10 skipped、1个既有warning**，193.12秒；
隔离QA服务已停止，诊断 `/tmp/v2-data-qa.N54HAf`。
Ruff检查、格式检查和git diff检查通过。测试使用临时PG/Redis，不连接新联调库；
上面的真实网络联调是另行执行并记录的，不能与模拟测试混称。

## 未完成与下一步

1. 明确旧测试仓/条件单的处置，记录旧系统移交基线，禁止混账。
2. 接真实账户保证金/下单事务风控、S6/S8执行与冷却/仓位上限。
3. 完整保护单生命周期及平仓、自动对账、费用/资金费归因与结算报告。
4. 策略交易贯穿测试网验收、崩溃恢复与启动恢复，不能用当前数据侧成功代替。
5. 完整部署需固定发布目录与独立锁定Python环境、持久service、最小DB权限、
   PG不可用时的外部告警、归档保留治理与重启演练。当前复用QA Python环境。

服务器另外存在三个来源未明的root systemd unit：BswCo7W1sTA、C7lfjwWDb27、
PX9nhIzLvCg，均指向不存在的随机命名程序并反复203/EXEC失败。未运行这些程序，
未修改这些非交易unit；尚不能据此认定入侵，但实盘凭据部署前应核实主机可信性。
系统 journald 也处于 failed 状态，不能假设日志可靠；本次不擅自修复系统级服务。
