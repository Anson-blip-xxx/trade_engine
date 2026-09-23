# Testnet 统一交易守护进程

## 目标与边界

`services/v2_testnet_daemon.py` 提供方向策略 V2 的显式装配工厂和有界监督循环。
它只接受 `BINANCE / SANDBOX / FUTURES`，把已经实现的恢复、保护、主动退出、
结算、行情、S6/S8 调度和开仓派发接到同一账户流水。构造过程不启动线程、
不访问网络、不发送订单。`services/v2_testnet_daemon_entry.py` 现提供可测试的
完整进程装配和 CLI；仓库只提供未安装的 systemd 模板，没有执行部署动作。

这不是生产上线许可，也不是收益保证。只有把真实 Testnet 依赖注入并显式打开相应
权限后，阶段才可写交易所；本节点的 QA 全部使用隔离依赖和交易所替身。

## 三个独立权限

工厂要求权限与 `GuardedOpeningSubmit` 的门禁完全一致，默认全部为 `false`：

- `enable_entries`：允许新的 OPEN 请求。开启时必须同时开启保护单写入。
- `enable_protection_writes`：允许恢复既有仓位并创建保护单，可在禁止开仓时单独开启。
- `enable_reduce_only_exits`：只允许 `CLOSE + reduce_only=true`，可在禁止开仓时单独开启。

过去单一开关会在关闭新开仓时一并阻断风险降低型平仓。现在开仓与只减仓权限在
`GuardedOpeningSubmit` 中分离；错误账户、环境、产品或非 reduce-only CLOSE 仍被拒绝。
这允许运维进入“禁止增加风险但继续保护和退出”的安全模式。

`services/v2_testnet_daemon_bootstrap.py` 新增启动配置契约。调用方必须逐项提供
`V2_ACCOUNT_ID`、固定 `SANDBOX` 环境、1–60 秒轮询间隔及三个小写
`true/false` 权限值。只要任一写权限开启，还必须提供与目标账户完全匹配的
`V2_TESTNET_WRITE_ACK=SANDBOX:<account_id>`；仅打开开仓而未打开保护会直接拒绝。
配置对象的公开摘要不包含 API key、secret、TG token 或配置文件内容。

可选 `V2_EXTERNAL_POSITION_EXCLUSIONS` 只用于 SANDBOX 中已明确不由 V2 接管的外部
持仓。它必须是规范、唯一且与 `V2_SYMBOLS` 不重叠的列表；配置和每次 coverage 证据
均公开记录。它不忽略外部挂单，也不能隐藏 V2 自有仓位，详见
[实际只读联调](V2_UNIFIED_DRY_RUN_20260923.md)。

## 进程入口与密钥边界

`TestnetProcessConfig` 只从进程环境读取非敏感设置：账户、SANDBOX、轮询间隔、
标的列表、退出费率及三项权限。只要环境中出现 Binance/TG 密钥字段就拒绝启动。
密钥只能通过 `--credentials` 指定绝对路径的普通文件，且文件必须非软链接、
大小有界、权限不得开放给 group/other。加载器只选取
`BINANCE_TESTNET_API_KEY/SECRET` 和 TG 通知字段，即使旧文件同时存在 LIVE key 也不会读取。

统一工厂显式装配隔离 PG、Redis 当前上下文、ClickHouse 行情归档、公共/私有配额、
签名 Testnet transport、不可变风险参考、账户回撤、归档驱动 S0、S6/S8 独立历史和 TG 外层告警。
构造本身无网络 I/O；私有路由白名单和 PG 分钟配额均先于 transport。

## 每轮顺序与持久证据

守护进程先提交 `testnet-trading-daemon-v1` 的 `STARTING` 检查点，PG 不可用时不启动
流水。随后流水按以下顺序执行：

1. 未知订单和超时任务恢复；
2. 已有仓位保护覆盖；
3. 主动退出与原生保护成交竞争恢复；
4. 成交、费用、资金费和最终结算；
5. T60 已收盘后验回补；
6. 行情采集；
7. 从已确认行情归档生成 S0，并完成 PG→Redis 投影；
8. S6/S8 正式策略调度；
9. 条件满足且开仓权限开启时，最多派发一笔新开仓并立即恢复、保护。

每个阶段前后另有 `trading-pipeline-cycle-v1` PG 日志。周期结束后守护进程提交
`RUNNING` 或 `DEGRADED` 检查点，其中记录账户范围、三项写权限、流水状态和周期身份。
`CYCLE_COMPLETE` 只代表该轮门禁正常，不能替代订单、成交和结算事实。

## 监督行为

循环间隔必须是 1–60 秒，停止端口必须同时提供 `is_set()` 和有界 `wait()`，从而支持
优雅停机。未处理异常只向通知端口发送异常类型、状态和账户范围，不外泄 API key、
请求正文或异常消息；通知失败不会杀死监督循环。真实部署仍需外部进程管理器检查
进程存活和 PG 完全不可达等进程外故障。

新增 `DaemonTelegramNotifier` 把上述受限事件映射为 `TRADING_DAEMON` 通知。该通道
特意不依赖 PG，因而能报告守护进程启动检查点失败；代价是它属于 best-effort，
不是业务 ACK，也没有恰好一次语义。错账户、非 SANDBOX、任意附加字段或非法错误码
都会在任何网络 I/O 前被拒绝。

## QA 与下一门禁

新增测试验证无 I/O 构造、真实生命周期阶段装配、LIVE/错账户/错环境拒绝、三权限
匹配、禁止开仓时允许只减仓、开仓必须有保护、动作前检查点、异常脱敏和有界停止。

结算后的自动 T60 采集、配置/密钥加载进程入口和 systemd 模板已完成。
模板位于 `deploy/v2-testnet/`，必须显式渲染 `@PROJECT_ROOT@`/`@PYTHON@`/
`@ENV_FILE@`/`@CREDENTIAL_FILE@` 占位符后才可安装；样例环境默认三项写权限全关。

V2 S0 已接入确认归档，详见 [S0 说明](V2_S0_REGIME.md)。进程不会伪造中性 regime：
缺失、过期或不一致 S0 时保护、退出、结算和行情仍可运行，S6/S8 准入失败关闭。
首次真实只读统一循环已经完成，数据见
[2026-09-23 联调记录](V2_UNIFIED_DRY_RUN_20260923.md)。仍需完成历史任务追平、
Testnet 长时间运行、真实自然信号全链路、
重启/依赖中断演练、可观测性和发布回滚。
账户、资金费、合约规则及 PG 历史已有真实 provider；
完成这些验收前不得切换实盘。
