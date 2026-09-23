# V2 Testnet 独立健康监督

## 目标与边界

`services/v2_testnet_watchdog.py` 是与交易 daemon 分离的只读监督进程。它没有 Binance
客户端，不加载 Binance key，不创建订单，也不写业务状态。它只读取固定本机依赖和
PostgreSQL 中已经由 daemon 提交的审计心跳。

watchdog 不把本地文件作为状态存储。异常通知的状态转换仅保存在进程内存中；进程重启
可能重复一次当前故障通知，这是 TG best-effort、at-least-once 边界，不影响任何交易
幂等性。权威运行事实仍是 PG 审计历史和各依赖自身状态。

## 探测与失败语义

每轮按固定代码检查：

- PostgreSQL 连接；PG 不可用时跳过心跳查询，报告 `POSTGRES_UNAVAILABLE`；
- Redis Unix socket PING；失败报告 `REDIS_UNAVAILABLE`；
- ClickHouse 本机 `/ping`；失败报告 `CLICKHOUSE_UNAVAILABLE`；
- `testnet-trading-daemon-v1/latest` 当前状态及对应不可变 history 提交时间；缺失、
  过期、降级或结构异常分别使用固定诊断码。

心跳年龄由 PostgreSQL `clock_timestamp()` 与 `v2_state_history.created_at` 计算，
不信任 watchdog 主机时钟。`STARTING` 和 `RUNNING` 只有在限定年龄内才健康；
`DEGRADED` 立即报警。自由文本、异常消息、端点和响应正文都不会进入通知。

状态不变时静默。新故障发送 `UNAVAILABLE`，已报告故障消失后发送 `RECOVERED`。
通知失败不会把状态标为已报告，下一轮重试。通知成功后持续故障不会周期刷屏。

## 配置和部署

非敏感配置来自 daemon 的 EnvironmentFile：

- `V2_ACCOUNT_ID` 与 `V2_ENVIRONMENT=SANDBOX`；
- `V2_WATCHDOG_INTERVAL_SECONDS`，范围 5–60 秒；
- `V2_WATCHDOG_HEARTBEAT_MAX_AGE_SECONDS`，范围 30–900 秒，且至少是轮询间隔两倍。

凭据文件沿用受保护配置，但加载器只提取 `TG_NOTIFY_TOKEN` 和
`TG_NOTIFY_CHAT_ID`。systemd 模板
`deploy/v2-testnet/trade-v2-testnet-watchdog.service.in` 不 `Requires` PG、Redis、
ClickHouse 或 daemon，因此依赖失败时监督进程仍可运行；启动前只核对锁定 Python
运行时，不等待被监督依赖 readiness。

## QA 与 2026-09-23 证据

- watchdog 相关隔离测试：`37 passed`，包含真实临时 PostgreSQL 心跳查询；
- V2 核心隔离套件：`1237 passed`；
- 全仓隔离套件：`4190 passed, 10 skipped, 1 existing warning`；
- Ruff 检查通过；
- 锁定 Python 3.12 runtime-only 预检及模块导入通过；
- 对当前 Testnet PG/Redis/ClickHouse 和 daemon 心跳做无通知、纯只读探测，
  返回空故障集合。

本节点没有发送 Binance 写请求，没有改变三项交易权限，也没有重启现有 daemon。
真实依赖中断通知、恢复通知、整机 reboot、长时间 soak 与回滚仍需单独演练，不能由
上述只读烟测替代。

部署后补充证据：`trade-v2-testnet-watchdog.service` 已安装并 `enabled`，使用只读 release
`1f83956dfabeda9725b8d5eb8e5522a3885d0f42` 与锁定 Python 3.12 venv；连续观察为
`active/running`、`NRestarts=0`。首次模板遗漏 `[Install]` 使 `is-enabled=static`，由
提交 `50f8c85` 补回并增加回归断言，重新安装时只执行 `daemon-reload` 和 `enable`，未重启
watchdog。原交易 daemon 的 `ActiveEnterTimestamp` 仍为 14:43:43，继续运行
`a29565a...` release 且 `NRestarts=0`；三项写权限仍为 `false`。

后续依赖中断演练见
[设置协调与分阶段写入验收](V2_TESTNET_SETTINGS_DEPLOYMENT_20260923.md)：在交易写权限
全关时停止 ClickHouse，daemon 按依赖关系停止而 watchdog 持续 active；恢复后依赖和
daemon 均 0 重启并重新达到 `CYCLE_COMPLETE`。本机没有 TG 送达回执，因此只记为探测
窗口覆盖，不宣称通知恰好一次或已由用户端确认。
