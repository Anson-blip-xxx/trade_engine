# V2 统一 Testnet daemon 只读联调（2026-09-23）

## 执行边界

本次使用独立 `16/tradev2 / trade_v2_testnet / trade_v2`、V2 Redis socket、V2
ClickHouse 和 Binance Testnet 凭据。三项交易写权限均为 `false`，没有开仓、保护单
创建、撤单或平仓请求。旧 market pilot 和 protection worker 已停止并由统一 daemon
替代；旧 main 服务未恢复。

凭据文件只收紧权限为 `0600`，加载器确认只选择 TESTNET key/secret 和 TG 字段，
没有打印字段值。统一 daemon 是 transient 单元，运行候选 release 和 venv 位于 `/tmp`，
因此本次临时关闭 `PrivateTmp`；其余 systemd 硬化保留。正式发布必须把 release/venv
安装到持久只读目录，再恢复模板中的 `PrivateTmp=yes`，不能直接复制本次临时例外。

## 发现与修复

隔离 PG 是早期 schema，缺少 `v2_directional_outcomes` 和
`v2_directional_followups`，导致 settlement/T60 返回 `UndefinedTable`。新增可重复执行的
`db/migrations/20260923_directional_history.sql`，先在同库事务中完整执行并回滚验证，
确认对象消失后再以单事务提交。历史 S3、订单和审计数据未清理。

账户仍有明确的外部 `ZORAUSDT` 仓位。根据既定“ZORA 不再处理”决定，新增
`V2_EXTERNAL_POSITION_EXCLUSIONS`：只允许 SANDBOX daemon 使用，配置必须是规范、唯一、
不与 V2 交易 universe 重叠的 symbol 列表。它只从仓位数量等式中排除指定外部仓位；
如果该 symbol 被 V2 本地账本认领，或出现任何未认领普通/条件单，仍然失败关闭。
每次 coverage 结果和不可变证据均记录实际排除列表。本次仅配置 `ZORAUSDT`。

## 实际结果

迁移和排除配置生效后，连续周期返回：

- protection `CLEAR`，coverage `ACCOUNT_COVERAGE_CLEAR`，证据含 `["ZORAUSDT"]`；
- exits、settlement、followups 均为 `CLEAR`；
- market 为 `CURRENT/ACKNOWLEDGED`，S0 为 `PROJECTED`；
- pipeline 为 `CYCLE_COMPLETE`，`entry_dispatch_enabled=false`；
- daemon 无重启，PG/Redis 中 S0 持续推进；
- 检查窗口内 `v2_orders.updated_at` 没有变化。

S6/S8 正在以持久 receipt 消费早期 S3 历史信号，旧信号按原有效期记录为 EXPIRED，
不复活为当前交易。没有为通过验收而注入固定信号或固定数量订单。

## QA 与未完成门禁

排除、迁移、生命周期及 daemon 聚焦 QA 为 74 passed；最终全仓回归为
4168 passed、10 skipped、1 条既有 PM golden warning。变更通过 Ruff、format 和
diff 检查。观察窗口累计 62 个连续 `CYCLE_COMPLETE`、0 个 `ENTRY_BLOCKED`、
0 次服务重启、0 条订单更新，S6/S8 各持久处理 620 条过期历史任务。

仍需更长时间运行观察、
历史任务追平后的自然新信号验证、依赖中断/重启演练，以及安装到持久 release 目录。
只有这些只读门禁稳定后，才可分阶段开启保护写入、reduce-only 退出，最后才考虑开仓；
每一步都必须保留独立权限和账户绑定 acknowledgement。
