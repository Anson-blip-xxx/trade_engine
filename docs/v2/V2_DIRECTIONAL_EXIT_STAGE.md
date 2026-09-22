# S6/S8 主动退出与保护单竞争恢复

`v2_core/directional_exit.py` 和 `services/v2_directional_exit.py` 将正式 S6/S8
Testnet episode 接入 PostgreSQL 权威退出生命周期。它不读取 Redis/本地持仓文件，
不复用旧 PM 的可变字典，也不取消已有交易所止损后再裸露平仓。

## 决策证据

每轮从不可变开仓决策取 `system_tag`、原始止损和计划风险，从实际 OPEN/CLOSE fills
重算加权入场价、剩余数量、首次成交时间及剩余风险。行情端口必须提供 10 秒内的
mark、funding、1h EMA、四根已收盘 15m close 和预估退出费率。
所有数值使用十进制字符串；缺字段、未来时间、过期数据或错误环境均 fail closed。

退出优先级为：不利资金费、原生止损边界等待、紧急损失、五分钟早期弱动量止损、
成本/风险单位归一化的低收益停滞、分层止盈、峰值回撤、盈利 1h 反转、时间止损。
S6A/S6B/S8 阈值来自原策略配置；不再用绝对 `1 USDT` 判断大小仓位的停滞。
峰值与分批状态保存在 `directional-exit-v1`，同一观察重复执行不增加版本。

`BinanceDirectionalExitMarket` 已把该端口接到同一受 PG 权重预算约束的无凭证 Binance
公开 transport：先核对交易所时钟，再读取 premium mark/funding、4 根完整 15m K 线和
20 根完整 1h K 线。请求窗口严格对齐已收盘边界，缺根、乱序、非字符串价格、时间偏移
或任意额外未授权 endpoint 都会失败。quantity step 不信任实时行情输入，而是从该
episode 不可变开仓 sizing evidence 取回；退出费率是显式部署配置并随观察落证据。

## 执行与恢复

默认 `allow_writes=False`：应退出时只落证据并阻断新开仓，不发送订单。显式开启后：

1. 先确认已登记 close-all STOP_MARKET 仍为 `NEW`；
2. 持有账户级 advisory lock，重新读取本地事实和完整交易所账户；
3. 要求持仓、普通单、条件单、所有权和保护条款全部一致，并保存 preflight inventory；
4. 先写退出证据、冻结该 episode 后续开仓，再创建确定性 CLOSE 订单；
5. 只发送 MARKET `reduceOnly=true`；超时进入 UNKNOWN，只按原 client ID 查询；
6. 下一轮先恢复所有已登记 CLOSE，终态后才重新执行账户覆盖审计。

主动退出期间不撤原生止损。若止损恰好触发，其交易所 child 已经存在，必须落入账本，
即使本地还有主动退出预留。`Orders` 只对完整 `BINANCE_ALGO_CHILD` 父子身份开放这种
重叠表示；普通调用或伪造不完整 evidence 仍被拒绝。两条路径均为 reduce-only，
不能反向开仓；其后仍需把另一订单查询到终态，覆盖审计在此之前持续阻断新仓。

## QA 与边界

定向 QA 覆盖多空符号、全部退出优先级、S6A/S6B/S8 分批比例、成本化风险单位、
旧 observation 幂等、默认禁写、PG 决策证据、部分止盈数量步进、reduce-only 请求、
ACK 后重启 GET 恢复、单次 POST、止损缺失、锁竞争，以及原生止损与主动退出重叠。

本节点仍是隔离代码和模拟交易所协议验证：公开行情端口已接线，但未部署常驻 daemon，
未使用真实 Testnet 策略信号触发主动退出，也未完成动态保本/移动止损替换。
发布前仍须完成真实自然信号
开仓→保护→主动/原生平仓→现金核验→最终结算的长时间运行和故障注入验收。
