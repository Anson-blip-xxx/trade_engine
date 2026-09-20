# V2 归档行情参考与受控交易端口接线

状态：代码及隔离验收，不是上线许可。没有读密钥、安装服务或真实下单。

## 价格来源

`ArchivedRiskReference` 只接受明确 AccountScope 的 OPEN 请求：

1. PG 查询本环境最新 candle delivery，不跳过未确认/隔离的新帧去取旧帧。
2. 要求 ACKNOWLEDGED、不可变确认审计和 S3 发布 input digest 一致。
3. 按 PG archive digest 读取归档，限制 8 MB，核对完整内容摘要。
4. 复用严格 1440 分钟输入校验/重算，核对 frame identity 和完整发布 input digest。
5. 取目标 symbol 最新已收盘分钟的原始 Decimal close，不取指标计算舍入后的浮点值。
6. 再读 PG 最新帧，读取中已切换则失败；再次检查时钟和期限。

参考 source 固定为 `binance-closed-1m-v1`，保留原 observed_at，绝不刷新为读取时刻。
有效期是来源批次期限、来源最大年龄、适配器最大年龄的最小值。`RiskReference`
携带不可变 frame/archive/input 身份和 valid_until；账户锁内再次检查该有效期，
即使账户 policy.max_reference_age_ms 更宽，也不能放宽适配器期限。

本路径不信任请求附带的 price，不用 Redis 单独证明来源，不回退文件或旧 symbol。
归档丢失/损坏/迟延、PG 不可用或来源不符会在发送前失败，尚未提交的订单保持
PREPARED，原有期限仍适用。适配器不自动重试，也不延长交易意图生命期。

摘要证明的是与 PG 确认内容一致，**不是对恶意 publisher 的密码学来源认证**。
部署仍必须约束采集程序、PG/CH 写权限及客户端端点；历史分钟 close 也不是实时
mark price、盘口价格或成交保证，不替代真实余额/保证金风险校验。

## 显式工厂

`create_guarded_runtime` 组合 DataRuntime、归档参考、账户绑定风险回调和交易所端口：

- 提交/查询不能跨 exchange/account/environment/product；不匹配的开仓检查拒绝。
- `enabled=False` 默认禁止提交；已证明未发送按既有终止逻辑释放预留。
- 开仓必须有匹配来源的显式 PG AccountPolicy；工厂不自动设置额度或激活配置。
- CLOSE 不读取价格归档，但仍经过绑定、外部风险策略和显式写开关。
- 客户端、submit/query/risk_check、clock 均为显式依赖；构造不联网、不读取环境密钥。
- ports 必须有有限超时；工厂不是可部署常驻 daemon，也不自动构造实盘 transport。

后续已补 [账户维护作用域](V2_ACCOUNT_MAINTENANCE.md)：本工厂传入 scope，恢复、
过期清理、超时告警、额度释放与财务投递均限制到该账户，发现/领取先筛选再 LIMIT。
scope=None 的底层兼容入口仍是全局维护；应用筛选不替代数据库最小权限或 RLS。
现有核心直接构造路径仍用于兼容 QA；真实策略迁移必须统一使用受控接线并消除旁路。

## 边界与成本

每次开仓参考会读取/重建完整有界批次，不在进程保存价格权威缓存；大 symbol 集合
成本较高，需要生产规模基准及后续按内容寻址的证明优化。不得以优化为由仅信任
可篡改/过期缓存。CH 集群贯穿、历史算法版本兼容、权限及实际行情在线验收仍未完成。

参考 [账户预算](V2_ACCOUNT_RISK.md) 与 [剩余门禁](V2_REMAINING_WORK.md)。

## QA 检查点

新增 36 项 QA：精确 close/完整来源证据，缺失/未确认/隔离回执、发布摘要不符、
归档丢失/错摘要/超时、symbol/账户/环境绑定、未来/过期/读取中到期及时钟回退、
读取中批次切换、不回退到旧帧、PG 锁内重新验证参考期限、默认禁交易及已知未发送
释放、跨账户查询阻断，以及行情归档→风控预留→模拟提交→trace 贯穿。
另验证 CLOSE 不读取归档，不因价格来源故障而阻止平仓。

最终全仓 **3498 passed、10 skipped、1 个既有 warning**，158.02 秒。
Ruff/格式/diff 检查通过；临时 PG/Redis 已停止，诊断 `/tmp/v2-data-qa.idS6UR`。
PG 为真实临时实例，CH 归档客户端及交易所端口使用替身，不是在线网络验收。
