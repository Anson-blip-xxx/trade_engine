# V2 信号与上下文入口

剩余清单第 1 项的代码实现检查点，**不是整项接线完成或上线批准**。
本轮完成独立 TradingView WSGI→PG 信号→策略调度接线、内部 s2/s3 接入契约、
s0/s2/s3 上下文校验及期限贯穿。旧生产服务没有切换。

## 数据流与确认语义

外部请求先认证、校验，再通过 `SignalIngress` 登记到 PG `v2_inbound_signals`；
任务调度依赖 PG 发现，不依赖 Redis 通知。PG 不可用、写入失败或提交回执不明，
HTTP 返回 503，调用方必须用原事件 ID、原内容重试。没有文件降级或成功假回执。

身份范围为 source/environment/event_id。来源和环境由进程构造时绑定，请求不能
覆盖。并发同内容只保存一条；同 ID 不同内容返回 409。已登记事件的精确重放可以
在过期后返回原 signal_id，但不更新 observed_at/截止时间，不复活消费回执或订单。
未登记的过期/未来事件拒绝，不能把接收时间当原始触发时间。

HTTP 200 `RECORDED` 只代表信号可靠登记（或原记录确认），不代表风控批准、
订单提交、成交或盈亏确定。`SignalIngress` 本身没有执行/交易端口。

## TradingView 请求契约

路径为 `POST /v2/webhooks/tradingview`，Content-Type 为 `application/json`。
最大请求体 16 KiB，必须有合法 Content-Length，不接受分块请求头。
严格 JSON：拒绝重复键、NaN/Infinity、浮点字面量和非法 Unicode；小数用字符串。
认证使用固定时序比较，密钥不进入业务记录，错误响应不回显请求或数据库异常文本。

必需字段：secret、event_id、observed_at、expires_at_ms、symbol、signal、price、strength。
可选字段：taker_buy_ratio、orderflow_bias、chg_15m、chg_1h、vol_1h。其余字段拒绝，
包括 source、environment、自由文本 comment；不能把凭据塞入审计上下文。

- event_id：1–128 位 ASCII 字母、数字或 `_.:/-`；生产者必须在首次生成时固定，
  重试不得重新生成，不用 symbol+signal 短期窗口替代真实事件身份。
- observed_at / expires_at_ms：整数 Unix 毫秒。期限由原始触发时刻确定，
  必须符合部署配置的最大年龄和最长有效期，不能用服务器当前时间补缺失值。
- symbol：接受规范合约名及 `BINANCE:BTCUSDT.P` 形式；规范化后只允许
  大写字母/数字加 USDT 或 USDC，拒绝其他交易所前缀和路径字符。
- signal：保留旧 TV 的九种名称映射（TREND/PULSE/VIOLENT/PUMP 多空及 PANIC_SELL_SHORT），
  但不接受旧 payload 自动补事件时间/身份的行为。
- price：正十进制字符串；strength：0–100 整数，不接受 bool。
  taker_buy_ratio 在 [0,1]、orderflow_bias 在 [-1,1]，也使用十进制字符串。
  chg_15m / chg_1h 是百分比变化，范围 [-100,10000]；vol_1h 是一小时内
  `(high-low)/low*100` 波动百分比，范围 [0,10000]。这三个字段仍是生产者证据，
  不能由接收时间或后来的市场场景补写。

旧 alert 模板没有稳定 ID/触发时间/期限，不能直接指向此接口；模板迁移须与
生产者接线一起验收。本轮未修改远端 TradingView 配置。

## 独立服务工厂

`services.v2_signal_ingress.create_application()` 返回 WSGI 应用。
导入模块不读配置、不联网；调用工厂不连接 PG、不启动 listener、不创建 schema。
以下配置全部必需，缺失即失败，无 main 配置或 public schema 回退：

| 环境变量 | 约束 |
| --- | --- |
| V2_SIGNAL_INGRESS_ENABLED | 必须精确为 YES；默认关闭 |
| V2_POSTGRES_DSN | 专用 V2 数据库连接，禁止记录到日志 |
| V2_POSTGRES_SCHEMA | 显式专用 schema，禁止 public / 系统 schema |
| V2_ENVIRONMENT | SANDBOX 或 LIVE；仅信号环境标签，不启用交易 |
| V2_TV_WEBHOOK_SECRET | 显式 32–512 字节秘密；长度校验不等于熵检验，必须使用随机秘密 |
| V2_SIGNAL_MAX_AGE_MS | 1–86400000，整数毫秒字符串 |
| V2_SIGNAL_MAX_LIFETIME_MS | 1–86400000，整数毫秒字符串 |

上线前仍需独立 WSGI 宿主、TLS 终止、请求/并发限制、有限读体超时、禁用请求体日志、
最小数据库权限和故障告警。此工厂不代替这些部署防护，本轮没有安装/启动服务器。

## 内部信号与上下文

内部 s2/s3 使用同一 `SignalIngress`，由可信进程绑定 source/environment，输入为
event_id、observed_at、expires_at_ms、symbol、signal、features 的规范对象。
这是进程内契约，不是无认证的公共 HTTP 入口。内部生产者不能把旧浮点/任意 raw
对象直接透传；需要在生产边界规范化，保留稳定事件身份和原始时间。

S0 作为市场上下文，不直接变成开仓命令。`ContextProvider` 接收显式只读且有超时的
`read(source, environment, symbol)` 端口；每个来源配置 GLOBAL 或 SYMBOL 范围及最大年龄。
快照必须携带 snapshot_id、source、environment、symbol、observed_at、features。
缺失/串环境/串标的/未来/过期快照均拒绝，不回退成空对象或默认风险可交易。
GLOBAL 范围明确使用 `*`，不能把其他币种快照当全局数据。

各来源读取结束后统一检查新鲜度，并复制为不可变决策证据。返回 assembled_at、
valid_until_ms、来源快照与新鲜度策略；年龄是整数毫秒且允许等于最大年龄，故
valid_until_ms = min(各来源 observed_at + max_age_ms + 1)，供严格 `< deadline` 检查。
`StrategyWorker` 再检查上下文范围/年龄，将意图截止时间限制为信号、策略等待期和
上下文有效期的最早值，dispatch 再核验。排队不能延长这三个期限。

已存决策的恢复无需读取新行情；无可用上下文时任务退避，信号到期后可以绕过
行情源正常终结。不是人工逐笔审批，也不会拿后来的场景重算旧决策。

## 尚未完成的第 1 项工作

- `services/tv_bridge.py` 仍是旧入口，未重定向、未部署新工厂。
- `s3/ports.py:S3RedisPublisher` 与 `strategies/s3_orderflow.py` 的生产输出仍需迁入
  持久信号及规范上下文。不能直接沿用吞错/latest-slot 覆盖语义。
- s0 生产快照仍需输出显式环境、身份和时间封装；s2 实际生产端位置及映射需继续定位。
- 已新增 `RedisMarketContext` 的隔离行情读写和 S0/S3 输出适配器，见
  [生产批次说明](V2_PRODUCER_PUBLICATION.md)；旧循环和持久生命周期仍未切换。
- 现有低层 StrategyWorker 仍支持测试/内部简单上下文；未来生产 worker 必须绑定
  经过校验的 ContextProvider，不能用空字典跳过生产上下文要求。

先完成这些生产者接线与回放验证，再推进第 2 项账户级原子风控；本轮不跨过门禁。

## 本轮 QA

新增 94 项（含参数化用例）：入口认证与严格 JSON、HTTP 边界、来源/环境绑定、
并发重试及冲突、PG 提交前失败与提交后回执丢失、过期重放、上下文范围/年龄、
截止时间贯穿到 dispatch、默认关闭的服务工厂及真实隔离 schema 接线。
WSGI 协议校验在内存内运行，没有开启网络 listener。

全仓 **3218 passed、10 skipped、1 个既有 golden test 返回值 warning**。
Ruff 与 diff 空白检查通过。隔离 PG/Redis 已停止，诊断目录
`/tmp/v2-data-qa.9UhZPQ`。未调用实盘、修改运行数据库、更新远端 alert 或重启服务。
