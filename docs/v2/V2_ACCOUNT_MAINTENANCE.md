# V2 多账户维护隔离

状态：代码与隔离验收；未部署，不涉及生产服务或真实交易。

## 边界

`DataRuntime`、`ExecutionRunner`、`RecoveryAttention` 和财务 `Projector` 支持
显式 AccountScope(exchange/account_id/environment/product)。`create_guarded_runtime`
必定传入其绑定 scope；不再仅在最外层交易所端口拦截跨账户请求。

- accept_open 在保存证据/意图前检查账户，错误绑定不产生新业务记录。
- 直接 dispatch/recover/snapshot 发现外部账户订单时抛 ScopeMismatch，在调用风控、
  取消订单或交易所 I/O 之前拒绝，不再把别的账户 PREPARED 单改成 CANCELLED。
- 订单恢复发现与租约领取都先过滤 scope 再 LIMIT。已有全局 worker 留下的任务也要
  重新过滤；外部账户任务的 attempts、lease、next_attempt、error 不因本账户执行而变化。
- 过期清理覆盖无订单的 RECEIVED 意图和 PREPARED 开单，不取消其他账户订单。
- 超时告警只扫描本账户订单；额度释放补扫只扫描本 scope 的 HELD 终态预留。
- 财务投递的直接/租约批次均先筛选事件所属意图，再发现/领取。不同账户可使用
  同名 consumer，事件 ID 及回执仍独立；不能互相领取历史失败任务或写对方回执。
- 账户 runtime 构造时拒绝未绑定或绑定到其它 scope 的 projector。行情运维 outbox
  仍按环境使用独立投递器，不伪装成带交易意图的账户财务事件。

同账户多个实例仍共享 PG 租约和令牌隔离；实例重启不依赖内存游标。所有过滤参数
使用绑定变量，不把账号内容拼进 SQL；SQL 表名和 alias 来自代码内固定结构。

## 兼容与安全边界

scope=None 保留旧全局核心/中央维护兼容行为，不能不加判断地部署成每账户服务。
部署工厂默认有 scope；新增策略必须走受控入口。直接使用 runtime.data、Orders、
Ledger 或连接对象仍属于底层/管理能力，并非受限账户 API。

这是应用调度隔离，**不是数据库 RLS 或权限安全沙箱**。拥有同库写权限的任意代码
仍可能绕过应用 API，最小权限、角色配置和禁止旧 executor 旁路仍是发布门禁。
注入 sink/query/submit 是可信依赖，必须具有有限超时。投递仍为 at-least-once，
scope 不改变外部接收方按事件 ID 去重的要求。

账户预算策略与参考来源见 [账户预算](V2_ACCOUNT_RISK.md)、
[归档参考及受控工厂](V2_ARCHIVED_RISK_REFERENCE.md)。真实策略、保护单和全账户
自动对账仍未完成，不能把维护隔离验收当作整套实盘上线验收。

## QA 检查点

新增 30 项 QA：四字段作用域、发现/领取在 LIMIT 前过滤、旧全局任务兼容、
并发跨账户与同账户领取、过期租约重建、直接错误订单 ID 不取消对方订单、
入库前拒绝外部意图、无订单/已准备订单的过期隔离、超时告警、额度释放、
共享 consumer 的直接/重试投递及财务回执隔离、构造时错误绑定拒绝和 tick 组合。
关联测试还回归独立运维通知和归档参考工厂。

最终全仓 **3528 passed、10 skipped、1 个既有 warning**，172.13 秒。
Ruff/格式/diff 检查通过；临时 PG/Redis 已停止，诊断 `/tmp/v2-data-qa.1TD6Lu`。
没有执行真实交易、更新运行数据库或重启现有服务。
