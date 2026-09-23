# V2 持久只读 Testnet 发布（2026-09-23）

## 不可变 release 与运行时

首次冷启动使用远端 V2 提交 `da0b0f7fbd7c48cb39bd01948925559d96cd21a4`。发现运行时
版本缺口并修复后，最终 daemon release 切换到：

`/opt/trade-engine-v2/releases/a29565a40219ca16e11fd2b41976dc259059d8ce`

目录由 root 持有并移除全部写权限。首次冷启动使用
`/opt/trade-engine-v2/venvs/py312-system-v1`，随后核查发现它继承的系统 psycopg 为
3.1.17，与 QA 锁定的 3.3.6 不一致。该运行时不再作为最终发布目标；后续节点改用独立
`/opt/trade-engine-v2/venvs/py312-locked-20260923-v1`，完整解析版本记录在
`requirements-v2-runtime-lock.txt`，启动预检在任何外部 I/O 前核对 Python 3.12 和所有
驱动/传递依赖的精确版本，不匹配即失败关闭。切换时实际服务账户执行预检 status=0，
systemd 的 `ExecStartPre` 也记录 status=0。release 不包含 `config/binance.env`，pytest
也不能向 release 写 cache，这是预期的无密钥、只读边界。

## 永久 systemd 栈

安装并实际运行：

- `/etc/systemd/system/trade-v2-cache.service`；
- `/etc/systemd/system/trade-v2-clickhouse.service`；
- `/etc/systemd/system/trade-v2-testnet-daemon.service`；
- `/etc/trade-engine-v2/testnet-daemon.env`（非敏感、三项写权限全关）。

daemon 通过 `Requires/After` 绑定独立 PG、Redis 和 ClickHouse，工作目录固定到上述
release，恢复 `PrivateTmp=yes`、`ProtectSystem=strict`、空 capabilities 等硬化。
只启用 daemon 的 multi-user target 链接；依赖由 `Requires=` 拉起，旧 market pilot、
protection worker 和 main 服务没有启用。

第一次永久栈冷启动出现一次 daemon 自动重启，证明 `After=` 只约束进程顺序，不能证明
依赖已能响应。随后新增 `services/v2_dependency_preflight.py`：最多 30 秒、固定端点、
无密钥地验证 PG cluster/database/schema、Redis PING 和 ClickHouse `/ping`；Redis 单元
同时改为 systemd notify。第二次完整停止 Redis/ClickHouse/daemon 后冷启动，三者均
0 重启，daemon 只在依赖可用后启动。

## 恢复和运行证据

Redis 使用无持久化投影。本次受控停止清空缓存后，S0 和 BTC/ETH S3 键均由权威
PG/ClickHouse 数据及后续确认帧恢复。恢复后保护、退出、结算、T60、market、S0 持续
通过，订单表没有更新，`entry_dispatch_enabled=false`。

持久 release 观察窗口记录 38 个 `CYCLE_COMPLETE`。其中另有一次 Binance 公共行情
`PublicMarketError`：market 返回 RETRY、regime 返回 `MARKET_NOT_CURRENT`，本轮
`ENTRY_BLOCKED`，PG 运维告警成功投递；后续周期自动恢复。这是预期失败关闭证据，
未触发订单写入或服务重启。

快照时 S6/S8 各完成 2590 条历史任务、无悬挂任务，S3 已有 5100 条信号；旧信号按原
期限形成 EXPIRED 决策，尚未完全追到实时头部。没有注入固定交易信号。

锁定运行时切换后 daemon 为 active/running、`NRestarts=0`，首批 4 个周期全部
`CYCLE_COMPLETE`；保护、退出、结算、followups 为 CLEAR，market 为 CURRENT 或
ACKNOWLEDGED，regime 为 PROJECTED。该窗口 `v2_orders.updated_at` 更新数为 0。

最终 release 解决了三个真实自然信号阻断：Demo 多空比路径只返回 `ok`，因此改用明确
记录来源的 LIVE 无凭据公共情绪端口；交易权限改由实际返回 `true/false/false` 的
`accountConfig` 核验；HIGH_VOL/LOW_VOL 等无关事件在外部上下文前直接终结。中间版本
实际落下 5 条 `sentiment_environment=LIVE` 账户上下文证据。最终版本上线后 S6/S8 均
达到 `completed=scheduled=source_signals=5262`、`incomplete=undiscovered=0`、
`caught_up=true`，连续 6 个周期 `CYCLE_COMPLETE`，0 订单更新、0 服务重启。该窗口
自然新事件为 HIGH_VOL/LOW_VOL，均形成 IGNORED/EXPIRED；没有注入方向信号。

## QA 与剩余边界

初始持久栈全仓回归为 4171 passed；运行时锁节点为 4174 passed；策略追平证据节点为
4175 passed；自然信号上下文修正节点最终为 4180 passed。后续各次均为 10 skipped、
1 条既有 PM golden warning；Ruff、format、diff 和完整 systemd unit 静态验证通过。

尚未执行整机 reboot，因此“已 enable”不等于真实断电启动演练。历史任务已经追平；
仍需自然出现的受支持方向信号只读判定、较长 soak、实际 reboot/回滚以及依赖持续故障
告警验证。完成这些
门禁前，保护写入、reduce-only 退出和开仓权限继续保持 false。
