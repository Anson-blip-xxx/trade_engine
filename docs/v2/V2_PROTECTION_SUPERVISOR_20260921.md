# Testnet 保护单常驻恢复巡检

## 能力和边界

`services.v2_testnet_protection_worker` 为已有 PG 保护登记提供持续恢复入口。
固定绑定 V2 Testnet 账户，签名传输关闭交易与撤单权限；网络只允许 GET
algoOrder / order / userTrades，并复用主机 PG 限额。不会开仓、创建/替换保护或撤单。

- 在 PG 按完整账户 scope 发现保护记录，并核对对应意图归属和 state identity。
- 复用原生子单接管：触发不等于成交，缺少子单/成交回报保持待恢复，不再 POST。
- 已取消/过期/拒绝的保护若仍有账本持仓，记录 UNPROTECTED；非终态订单标为
  EXPOSURE_UNCONFIRMED，不能把零已知成交当成确定无风险。
- 查询结果不变不再写保护历史；已结算且核验完成的终态保护退出轮询。
- 公平循环游标保存在 PG；一条坏记录不会一直占据第一页。异常只保存类名。
- 每轮最多 20 条，15 秒间隔；SIGTERM/SIGINT 在当前处理结束后停止，未处理项不前移。
- 与固定协议测试共享账户 session advisory lock。持锁期间不保持跨网络数据库事务。
- 子单接管提交后、checkpoint 前崩溃可以重新发现并幂等执行；不依赖本地文件。

这仍不是完整 PM。仅扫描已有保护登记和可关联的 V2 意图，不发现未登记裸仓或
未知交易所订单；终态平仓判断是账本状态，不是完整交易所账户清点。
不处理自动保护创建、动态撤换、所有退出竞争，也不自动结算恢复出的任意 episode。
这些缺口仍必须由后续 PM、账户对账和策略接线覆盖；本服务不授予新开仓许可。

## 告警

新增 `PROTECTION_RECOVERY` 运维事件，PG outbox 按账户、保护状态、结果和五分钟桶合并。
Telegram 投递使用原有带租约/重试/回执机制，消费者只领取本账户事件。
消息仅包含状态、保护 state_id、错误类等白名单信息，不包含密钥或原始交易所错误。
通知是至少一次语义；重复投递可按 event_id 识别，通知不能作为交易批准。

`--notify` 使用现有配置中的 TG 字段；正常且未变化的保护不会制造通知。
PG 全部不可用时无法持久投递告警，目前只有进程的脱敏 UNAVAILABLE 日志；
独立外部存活/数据库监控仍待完成。告警不会自动解决裸仓风险。

## 数据库升级

新库定义和 `db/migrations/20260921_protection_recovery_alert.sql` 一并提供。
在显式 V2 search_path 的一个事务中扩展 outbox 类型约束，5 秒锁等待上限；
失败事务回滚，不删除历史。重复应用测试通过。
实际已应用于 `16/tradev2 / trade_v2_testnet / trade_v2`，原 26 条事件保留。
不要在旧库或未知 search_path 上执行；回滚代码可保留这个向后兼容约束扩展。

## 真实只读验收

2026-09-21 执行两轮 `--once --notify`：

1. 发现 5 条关联保护记录；1 条原生子单 FILLED，4 条 EXPIRED 且账本已平。
2. 5 条结果已写入 PG；第二轮没有待处理记录，通知 claimed/delivered/failed 均为 0。
3. V2 库仍为 8 个订单、8 笔成交，没有新增测试仓位，也没有补发平仓单。

该验收没有人为制造真实故障通知；新事件的实际 TG 发送未单独注入验证。
TG 协议白名单、账户隔离、回执和重试使用隔离 QA；原 TG 通道此前已实测。

## QA

保护恢复/子单/协议专项 47 项通过；后续巡检+账户通知专项 49 项通过。
覆盖并发账户锁、跨账户过滤、游标重启公平性、坏记录隔离、接管后 checkpoint
失败重放、正负状态分类、告警合并和消费者隔离、迁移重跑、停止信号、消息脱敏。
最终全量：**3858 passed / 10 skipped / 1 历史 warning**，250.79 秒。
命令：`env V2_QA_PYTHON=/tmp/v2-qa-python.0ATk4H/bin/python3 bash scripts/qa_v2_data_core.sh -q`。
隔离诊断 `/tmp/v2-data-qa.gvHAd2`；Ruff、格式检查和 `git diff --check` 通过。
历史 warning 是旧 PM golden 测试返回 bool；跳过项不算通过。

## 启动

一次性核验：

```sh
/tmp/v2-qa-python.0ATk4H/bin/python3 -m services.v2_testnet_protection_worker \
  --config /home/ubuntu/.opencode/trade/trading_engine/config/binance.env --once --notify
```

省略 `--once` 持续运行。配置路径为部署选择，不应在日志打印文件内容。
停止此只读巡检不会撤销交易所保护，也不会停止独立行情采集。
当前测试环境仍依赖 `/tmp` 工作树和 QA venv，不是可重启交付的正式发布布局。

已启动临时 systemd 单元 `trade-v2-protection-worker.service`，15 秒轮询，启用 `--notify`。
User=ubuntu、NoNewPrivileges=yes、ProtectSystem=strict、MemoryMax=256M、CPUQuota=25%、
TimeoutStopSec=45、Restart=on-failure；初始检查 active/running，NRestarts=0。
日志 `/var/log/trade-engine-v2/protection-worker.log`；本地文件仅日志，不承担业务状态。
日志轮转及机器重启后的持久发布仍属部署门禁。未修改或重启旧交易服务。
