# S6/S8 纯规则与精确仓位计算

本检查点实现 `v2_core/directional.py`，迁出原策略的无副作用市场判断与
`shared_executor` 的历史表现、预期盈亏比、资金费率规则。**尚未接入常驻策略调度，
候选结果不是交易许可；不宣称 S6/S8 全部迁移或可以实盘。**

## 已实现

- S6：四类多头信号、VIOLENT_BULLISH 市场状态过滤、左右侧判断、S6B 接管、
  ATR/延伸过滤、评分、杠杆、保证金模式与止损比例。
- S8：五类空头信号、确认强度、PUMP_DOWN 高周期保护、左右侧判断、
  ATR/延伸过滤及对应评分与参数。
- 历史表现：六笔样本门槛、胜率/质量组合过滤、T60 复盘过滤；显式 hard/soft，
  soft 降权 0.5。成功加载的空历史和读取失败不能混同。
- 仓位：80% 资金池、3%..15% 评分分配、ATR 衰减、1% 计划止损风险上限、
  可用保证金/数量/名义价值上限、回撤与历史降权、数量步长和止损 tick 对齐。
- 执行前市场条件：R:R 最低 1、对当前方向不利的资金费率阈值 0.001。

`evaluate_market` 复用安全的 `decision.core` / `risk.core`，不导入旧 S6/S8 或
shared_executor；没有文件、网络、环境变量、密钥、系统时钟或状态写入。
市场指标判断沿用旧 float 规则；资金计算使用有界 Decimal 与 Fraction，
数量在精确有理数计算后向下对齐，避免重复除乘导致边界数量多/少一步。

## 有意收紧的行为

1. 缺失指标、非有限数、浮点金额、非法比例/布尔/时间直接报错，不填零后继续。
   金额/指标契约为十进制字符串；可缺失 flow/short ratio 必须显式 None。
2. 事件 flow=0 保留为有效的全卖方数据，不再被旧 `>0` 回退规则覆盖。
3. 移除旧计算的最低 10 保证金抬升及交易所最小名义额抬升。
   降权或预算不足以满足最小量/名义额时返回 INSUFFICIENT_BUDGET，不放大仓位。
4. 止损 tick 向入场方向对齐；若对齐到入场或另一侧则拒绝，不扩大计划风险。
5. 历史过滤不提供隐式关闭或异常放行。R:R 的显式零预期延续值仍表示旧规则的
   “没有预测，不做此项过滤”；缺失/坏值不能变成零。

## 调用顺序与证据要求

先对来源/时间/环境/账户进行校验并冻结上下文，再 evaluate_market；
有效候选经 analysis_adjustment 得到 factor，传给 size_candidate，最后做
execution_market_gate。调用者必须将原始输入、配置和所有结果存入已有 PG
DecisionEvidence，而不是只保存最终数量。该文件没有新增本地状态存储。

这些函数不会获取余额或自行确定数据真实性。外部提供的 balance/available_margin、
used_pool_margin、drawdown_factor、品种精度、费率与历史统计必须来自绑定来源，
具有原始时间和有效期；计划损失不含手续费、滑点、跳空，不是实际最大亏损保证。
超出 PG NUMERIC(38,18) 的输出拒绝，不能悄悄截断证据。

## 持久化回放接线

`services/v2_directional_replay.py` 提供显式、账户绑定的 StrategyWorker 工厂，
执行上述完整纯规则链路，将输入、配置、候选参数、历史降权、数量和拒绝原因
原子保存到已有 PG 决策证据表。重启/重试复用第一次提交的决定及原始期限。

该工厂只提供 REPLAY_ONLY：所有结果都是 IGNORED，不创建 intent/order，
并明确保存 execution_authorized=false；没有“开启实盘”参数。消费者使用
s6-replay / s8-replay，不占用将来 s6 / s8 的正式回执。不是历史时钟模拟器：
已经过期的源事件仍由 StrategyWorker 拒绝，不会刷新场景时间。

上下文沿用原有 assembled_at/valid_until_ms/environment/symbol/sources，新增
account_scope（exchange/account_id/environment/product）与 directional 快照。
directional 包含 market、price、regime、short_ratio、history、sizing、
expected_move_pct、funding_rate；sizing 不得覆盖 price/atr_pct/analysis_factor。
S3 的整数 strength 和市场整数指标无损转换为纯规则的十进制字符串；布尔/浮点
不会因此变成有效金额。其他方向及 HIGH_VOL 等事件明确忽略，不作为异常重试。

这是一条真实 PG 持久化决策回放链，但余额/行情/历史上下文提供器仍是显式注入，
不是在线来源验收。尚未启动回放常驻进程或让它读取运行账户。

## 仍未完成的真实策略接线

- PG 原子策略仓位数、信号/品种/平仓冷却、账户回撤峰值和恢复状态。
- S0 市场许可、真实资金/品种元信息/资金费率/滚动统计的可信提供器与期限贯穿。
- 将上述完整快照接到 StrategyWorker/Scheduler，所有开仓统一账户预留及受控端口。
- 恢复模式替换旧仓必须走持久化关闭流程；不能在纯 evaluator 内调用旧平仓函数。
- 杠杆/保证金设置、保护单确认与失效处置，以及 PM/自动账户对账。

不能把 CANDIDATE、SIZED 或 MARKET_GATES_PASSED 直接转换为生产放行开关。
旧运行入口没有被修改，新模块目前是下一步持久策略集成的基础。

## QA

新增 112 项无网络测试，覆盖九类信号、强度/ATR/延伸/接管边界、历史阈值、
R:R 与费率精确边界、输入不变性、非法/缺失输入、最低额拒绝、双重降权、
数量/名义上限、tick 对齐及调用方 Decimal 精度改变。
另新增 22 项隔离 PG 回放 QA，覆盖完整证据、并发、提交后中断、重启/换配置后
不重算、原始期限、非法上下文不落决定、账户绑定、拒绝原因、正式消费者隔离，
以及无法通过回放配置放行交易。两组共 134 项通过，诊断 `/tmp/v2-data-qa.8pXSgx`。
这些是规则、算术和持久化回放 QA，不是历史收益验证或实盘 E2E。
最终全仓 3662 passed、10 skipped、1 个既有 warning，182.60 秒；
临时服务已停止，诊断 `/tmp/v2-data-qa.WCenFL`。静态和格式检查通过。
