# 统一交易循环：接线进度与验收边界

## 当前实现

`services/v2_trading_pipeline.py` 将账户恢复、保护、退出、结算、市场采集、
S0 全局状态、S6/S8 决策和受控订单派发组织为同一个有界循环。仅接受 SANDBOX、同一账户
运行时、真实 directional-admission-v2-1 消费者及 GuardedOpeningSubmit。
默认不派发新单，不包含生产启动命令，不改变现有服务。

每轮使用 PG 账户级互斥锁；每个阶段开始前、结束后保存
`trading-pipeline-cycle-v1` 状态。PG 登记失败不启动后续动作。
持仓相关阶段先于行情采集，即使行情超时也不会因此跳过已有仓位管理。
保护、退出、结算必须明确返回 CLEAR；行情必须确认当前帧，随后
[S0](V2_S0_REGIME.md) 必须从确认归档写入 PG 并成功投影 Redis；缺失、异常或
不确定结果禁止新开仓。
派发一笔可能已受理的订单后立即恢复成交并调用保护，本轮不再派发第二笔。
未知成交不是可重试的新开仓指令，仍由现有持久订单恢复机制处理。

`services/v2_directional_context.py` 连接已校验的 S0/S3 行情上下文与账户、
合约精度、资金和历史分析输入。验证账户、标的、时间和证据，取最早有效期限；
不把缺失历史当成零交易，不把缺失 S0 当成中性，不刷新原始行情时间。
这只是输入契约校验，不认证任意注入账户来源的真实性。

`CYCLE_COMPLETE` 只表示本轮阶段返回正常，不代表成交、平仓或财务结算成功；
`entry_dispatch_enabled=false` 时即使有 PREPARED 也没有发送订单。
循环日志不是交易事实替代品；订单、成交、保护和结算仍各有 PG 权威记录。

## QA 证据

流水原有及本节点新增测试覆盖：

- 实际 S3 检测规则 → 上下文 → 实际 S6/S8 规则 → 不可变决策 → PG PREPARED；
  再运行不重复建单。
- 行情 HTTP 响应格式 → 实际采集器/校验/归档组件 → 持久数据源 → S3 →
  Redis → S6/S8 决策。策略允许拒绝信号，不为通过验收强制开仓。
- 行情故障仍执行管理阶段；缺失/过期/错账户上下文不建单；阶段未就绪阻断；
  PG 首次登记失败不执行阶段；副本互斥；派发超时/UNKNOWN 后立即恢复和保护。

使用隔离的真实 PostgreSQL、Redis；行情 HTTP、ClickHouse 客户端、账户
和管理阶段在这些新增用例中仍使用明确的 QA 替身。S0 已在模拟归档贯穿中执行
真实分类与 PG/Redis 发布。派发结果测试注入状态，
不是 Binance 成交证据。不得将这些测试称为真实 Testnet 全链路验收。

本节点最终全仓回归：4165 passed、10 skipped、1 条既有 PM golden 返回值警告，
402.83 秒。诊断目录 `/tmp/v2-data-qa.AJVCBx`；变更文件 Ruff/format/diff 检查通过。

## 仍需完成，不能替换成空操作或固定下单

1. S0 和账户/交易规则/资金/历史 provider 已接入 daemon；仍需真实 Testnet 依赖长跑、
   故障恢复和阈值历史回放，不能用隔离替身 QA 代替在线验收。
2. 保护阶段的实际 stage 已实现，见 `V2_DIRECTIONAL_PROTECTION_STAGE.md`：
   DirectionalStopRecovery、ProtectionSupervisor 和账户覆盖核验已串联，
   并已替换部分集成测试的保护替身。仍未部署为启用写入的常驻服务。
3. 退出阶段：接入实际 PM 退出决策、reduce-only 执行及保护单清理/竞态恢复。
4. 通用结算与分析阶段：连接成交、手续费、资金费、净盈亏及策略归因。
   现有固定 protocol-test 专用结算器不能作为通用实现。
   已补方向策略现金核验及调度接口，见 `V2_DIRECTIONAL_CASH_AUDIT.md`；
   CASH_MATCHED 仍不等于 SETTLED。最终钱包/仓位核对及资金费原子入账已由
   `V2_DIRECTIONAL_FINAL_SETTLEMENT.md` 接入；尚待真实 Testnet 与部署验收。
5. 真实依赖工厂、Testnet 守护进程、密钥边界和 systemd 模板已具备；仍需渲染到
   隔离环境并完成耐重启发布、真实 TG 告警和依赖故障演练。
6. 真实行情自然触发一次策略交易，从 frame/signal/evidence/intent/order/fills
   追到平仓、SETTLED、分析记录；核对计划与交易所实际杠杆/保证金模式。
   没有自然合格信号时等待，不以手工固定数量开单冒充。

真实全链路验收尚未完成。本次未向 Binance 提交订单，也未重启部署服务。
