# 测试网真实账户核验与提交入口

本节点限定 USDT、单向、单资产模式及无 V2 管理仓位账户的首单，不是多仓保证金引擎。
新增 `TestnetVenueReadiness`、`TestnetSymbolSettings`、`GuardedOpeningSubmit`，并通过
`bind_directional_venue_gate` 接入已有 S6/S8 运行时的 submit 端口。
绑定默认禁用，不启动循环；只有 SANDBOX 开仓权限显式开启后，才允许按策略计划设置
1–5 倍杠杆和 `ISOLATED/CROSSED` 保证金模式。

## 核验与落库

- 持仓前后读取、普通挂单、条件挂单、账户余额；任何已有仓位/挂单均阻断。
- 只读 accountConfig 检查 canTrade、单向及单资产模式；symbolConfig 检查实际
  杠杆、保证金模式、自动加保证金与名义价值限额，必须匹配 PG 原始策略计划。
- 用新鲜 mark 估算初始保证金并增加 10% 缓冲，同时检查 USDT 资产级可用余额、
  钱包与保证金余额；不能拿其他资产或全账户汇总余额补足缺失 USDT。
- 新增检查在订单 SUBMITTING 已提交后、真正调用 submit 前进行；本地未平账本、
  其他未知/活动订单阻断。账户锁串行化合作执行路径；精确订单身份和原始期限再次核验。
- PG `opening-readiness-v1` 保存完整观察证据；证据提交成功且未过期才调用下单端口。
  核验失败明确标记未发送；下单端口抛异常仍按 UNKNOWN，不自动重下。
- 实际配置不匹配时，先确认账户无 V2 管理仓位和任何普通/条件挂单，再把期望配置及
  inventory 摘要登记到 PG `venue-symbol-settings-v1`；之后才允许单次 POST。无论 POST
  返回、超时或异常，都必须重新 GET `symbolConfig`，不根据响应猜测、不盲重试。
- 杠杆/保证金设置拥有独立 transport 白名单和参数全集校验，仅 SANDBOX 可启用；LIVE
  构造直接失败。最终配置再次进入 opening readiness 证明，未核实绝不调用下单。
- 明确配置的外部持仓排除贯穿 inventory、设置与 readiness，但只排除持仓数量；任何
  挂单、采集期间仓位变化、V2 本地认领或尝试交易被排除标的仍失败关闭。

接口来源：[Binance 官方账户接口](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)。

## 真实 Testnet 只读验证

使用现有配置中的 TESTNET key，9 次私有 GET 加公共 mark 查询，未发任何交易所写请求。
观察已写入独立 V2 PG：`opening-readiness-probe-v1 / 968ecea1-3a3c-4e34-8322-32835211e18f`。

- 账户 canTrade=true。
- BTCUSDT 实际 leverage=1、marginType=CROSSED、isAutoAddMargin=false。
- 检查计划 2 倍 ISOLATED，与实际配置不一致；加上已有仓位，结果 BLOCKED。
- 获取区间 867 ms。未修改账户配置，ZORA 未操作，未新增仓位。

这是接口与阻断分支真实验收，不是自动开仓或盈利验收。

## 保留的边界

REST 非原子快照；外部操作仍可能发生在读取之后。10% 是估算缓冲，不保证覆盖跳空、
滑点、手续费。检查截止时间覆盖到调用 submit 前；下游端口内部额外 I/O 仍需未来
传输级期限检查。尚未强制所有其他调用路径使用此入口。
保护就绪、常驻 PM、通用自动结算、账户迁移/恢复及完整真实策略闭环仍未验收。
不能因 canTrade=true 或本节点 QA 通过就开启实盘。外部持仓排除必须显式配置并保留
审计证据，不能泛化成忽略未知账户状态。

## QA 结果

2026-09-23 新增设置协调、外部持仓排除一致性及 transport 白名单故障测试；定向
**125 passed**，完整 V2 核心 **1265 passed**，全仓 **4218 passed / 10 skipped /
1 warning**。覆盖 PG 先登记、非空账户阻断、超时但
已生效、超时且未生效、LIVE 拒绝、参数越界、被排除标的拒绝以及挂单/仓位变化不被
排除。修改的 Python 文件 Ruff 检查通过；本节点尚未改变运行服务的写权限。
