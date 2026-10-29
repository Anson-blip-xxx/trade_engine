# The Quant Trading Vault：全库盘点与 V2 适配评估

审查日期：2026-09-28，UTC+8。

## 结论与审查边界

可以作为研究素材库，不能作为已验证的策略产品直接接入。建议研究 **8 类能力（5 类入场、3 类公共组件）**，第一批选其中 4 类；目前可不经改造和验收直接上线的策略为 **0**，本轮完成盈利验证的策略为 **0**。

全库静态盘点覆盖 5,806 份条目；完整阅读了下文列出的 20 份代表性源码，并对照 V2 当前代码。选择样本用于识别适配方式和具体缺陷，不是随机抽样，不能由样本缺陷率推算全库缺陷率。没有逐个编译、执行、回测 5,806 份代码，也不能声称已完全理解每一个变体的交易行为。

本轮仅获取公开仓库、静态分析和编写本报告；没有安装 AgenKit、执行第三方脚本、读取账户密钥、下单、改数据库、改策略参数、重启或发布服务。分析程序和下载副本位于隔离目录 `/tmp/quant-vault-audit.YkVtXU`，不是生产代码或业务数据存储；本报告保存在 V2 工作区。

## 1. 仓库实际提供了什么

审计固定版本：[c9d6fa49486855899a92fea65004f440533049aa][vault]。V2 对照版本为 `12c1ed3`；本报告不是再次验收当前运行服务。

版本树只有根 README 和 strategies 目录；5,808 个文件均为 Markdown，包含两个 README 和 5,806 份策略/指标/工具条目。没有配套测试目录、依赖锁文件、统一回测引擎、行情数据集或可直接接入 V2 的策略包。

每份条目的 Detail 都指向 FMZ。条目修改日期字段分布于 2014—2025 年，不能把 2026 年的仓库提交时间当成策略研发时间。源码混合 Pine v1—v6、FMZ 平台脚本和其他交易所旧 SDK；Python/JavaScript 标签不代表可独立运行。

| Source 标签 | 实际策略文件数 | 索引条目数 |
| --- | ---: | ---: |
| PineScript | 5,283 | 5,286 |
| JavaScript | 362 | 362 |
| Python | 131 | 132 |
| MyLanguage | 27 | 27 |
| C++ | 3 | 3 |
| 合计 | 5,806 | 5,810 |

索引比实际文件多 4 个不同的 FMZ ID：289308、362014、380255、380258；不是重复索引。这解释了首页语言统计与文件总数的不一致。

源码重复度：

- 对 Source 区域代码块逐字哈希：5 组重复，12 份文件，7 份冗余副本。
- 仅去除 `/*backtest ... */` 注释、空行、行尾空白再哈希：66 组重复，140 份文件，74 份冗余副本。
- 此方法不识别改变量名、改参数或相近公式的语义重复；不能把剩余文件数当成独立盈利因子数。
- 例如首页推荐的 Adaptive Intelligent Grid，与另外 4 个标题不同的网格条目具有相同的上述规范化源码。

源码全文标记扫描还发现：731 份含 security 调用、73 份含 lookahead_on、187 份含逐 tick 计算开关、91 份含 Heikin-Ashi/Renko 调用。它们是需要复核的标记，不是已确认的作弊或未来数据泄漏数量。尤其高周期请求使用正确的历史偏移与 lookahead_on 组合，可以避免重绘，不能单看关键词定罪。[TradingView 官方说明][repainting]

849 份代码含 commission 参数赋值，225 份含 slippage 参数赋值；这不等于其余都没有成本设置（可能依赖平台设置），也不等于命中的已经正确模拟真实成本。5,270 份含 FMZ 格式回测配置注释；配置日期和交易对不是可复现的收益报告。

## 2. 许可和来源不能跳过

根 README 声称 MIT，但固定版本文件树没有 LICENSE 文件，其链接检查返回 404。源码文本中识别到 1,413 份 MPL、19 份 GPL、26 份 MIT，另有 15 份 CC BY-NC-SA 和 1 份 CC BY-NC 标记；不同标记可重叠。4,333 份未匹配此次扫描的许可模式，不能据此断言没有授权，也不能推定可自由复制。

例如 [Chandelier Exit][r06] 保留 GPL-3.0 声明；[SuperTrend trailing stop][r05] 保留原作者 MIT 授权文本。条目 Author 字段也不一定等于代码头中的原作者。后续逐文件保存来源、版本和许可证据，确认适用义务；未厘清前不直接复制进计划商业化的 SaaS。参考通用数学方法另行实现，也不能据此保证已经解决全部知识产权问题。

## 3. 全库研究线索，不是可用策略数量

以下按文件名关键词匹配，可交叉命中、不可相加，也不表示逐份通过审核：

| 家族 | 条目数 | 与 V2 的关系 |
| --- | ---: | --- |
| Donchian / Turtle | 74 | 单品种趋势突破候选 |
| Squeeze | 15 | 波动压缩后释放候选 |
| Supertrend | 176 | 趋势方向、回撤入场与退出对照 |
| Chandelier | 6 | 退出组件优先于新建交易机器人 |
| ADX / Choppiness / Regime | 70 | 单品种市场状态判断 |
| ATR 且含 stop/trail | 68 | 波动适应型退出 |
| VWAP | 49 | 锚定均价与偏离因子 |
| Mean reversion | 82 | 仅在经验证的震荡状态研究 |
| Grid / Martingale | 86 | 不进入当前方向性交易第一阶段 |
| Arbitrage / Pair trading / Market making / Hedging | 60 | 需要另一套多腿或库存管理能力 |

第一优先级四类标题集合（Donchian/Turtle、Squeeze、Chandelier、ADX/Choppiness/Regime）去重后有 **165 份研究线索**。不是 165 份“可以采纳”或“能赚钱”的策略。

## 4. 建议采纳的 8 类能力

采用的是经验证后的规则/组件，不是原执行脚本。P1 是首批研究范围，P2 是后续对照实验，不自动启用。

| 编号 | 能力 | 类型 / 优先级 | 参考与迁移要求 |
| --- | --- | --- | --- |
| A | Donchian 突破 + 相对成交量 | 入场 / P1 | [R03][r03]；突破基准必须排除当前 bar。成交量因子单独验证，不增加不可解释的多重门槛。 |
| B | BB/KC 压缩释放 + 动量 | 入场 / P1 | [R04][r04]；提取压缩状态迁移与方向，不搬逐 tick 与默认满权益设置；动量归一化，适配不同币价。 |
| C | EMA 中轴 + ATR 通道突破 | 入场 / P2 | [R02][r02]；已收盘信号思路可取，但原单次固定 10 个单位和 FMZ 下单/状态必须弃用。 |
| D | Supertrend 趋势/回撤 | 入场 / P2 | [R05][r05]；与 A/C 同属趋势类，先对照而不是都投一票加仓；处理假反转及过度交易。 |
| E | 有状态约束的 VWAP 偏离回归 | 入场 / P2 | [R07][r07]；原累计起点随历史长度变化，需明确滚动或锚定会话；补止损、时限、费用门槛，强趋势禁用。 |
| F | ADX / Choppiness 市场状态组件 | 公共组件 / P1 | [R19][r19] 的 DMI/ADX 和 [R08][r08] 的 CI 作为参考；用来路由策略而非要求所有策略全部满足同一个阈值。 |
| G | ATR / Chandelier 动态退出 | 公共组件 / P1 | [R06][r06]、[R18][r18]；与既有部分止盈、时间退出、原生止损共同评估，不绕过保护单生命周期。 |
| H | 止损距离 / 波动率风险定仓 | 公共组件 / P2 | [R15][r15] 只借鉴风险预算除以止损距离；V2 已有同类约束，重点是指标一致性与回归测试，不重复建设或引入 500 次叠仓。 |

优先顺序：先完成标准指标与回放基础，再研究 F/G，随后 A/B；C/D 做趋势基线对照，E 后置，H 与现有资金引擎合并验证。8 类能力不是承诺上线 8 套独立策略。

目标对应关系：A/B 研究当前脉冲启动及加速机会；F 减少错误市场状态下的使用；G 研究降低低产出持仓时间、减少过早截断大趋势。收益改善是待验证假设，不是本轮已证实结果。

## 5. 20 份代表源码的具体审查

| # | 来源 | 实际发现与决定 |
| --- | --- | --- |
| R01 | [Python Multi-Asset Momentum][r01] | 实为价格跨 5% 阶梯后买/卖部分库存，非跨币种强弱排名；固定三组数组、FMZ 隐式运行时和周期性撤全部普通单。不能直接移植执行器。 |
| R02 | [Multi-Variety ATR][r02] | 收盘突破 EMA±ATR、回到中线或触及损失阈值退出；声明 openRatio，但开仓实际固定 10 单位；依赖 FMZ 模板和 _G 状态。保留通道规则研究。 |
| R03 | [Donchian + ATRSL][r03] | 使用上一根通道上界突破做多、跟踪线退出；没有完整组合风险与成本模型。最清楚的 P1 入场参考之一。 |
| R04 | [Squeeze Momentum][r04] | BB/KC 释放配合线性回归动量；逐 tick 模式、可选早入场分支与默认 100% 权益需重构。只取已确认 bar 上的状态机。 |
| R05 | [SuperTrend trailing stop][r05] | 有原作者 MIT 文本；含逐 tick、订单成交后重算、软件平仓和信号价当入场价。趋势因子可研究，不能替代交易所原生保护。 |
| R06 | [Chandelier Exit][r06] | 有 GPL-3.0 文本；声明 study 又调用 strategy.entry，不能当作已验证可编译的 Pine 策略。借鉴数学，不直接复制。 |
| R07 | [VWAP mean reversion][r07] | VWAP 从样本起点累计、标准差却用滚动 20 根；无硬止损。需要明确锚点、状态与风险，后置。 |
| R08 | [Choppiness K-line][r08] | CI 形态可参考；交易时间固定为 09:15—14:55，非 24/7 加密交易口径。分离 CI 因子、处理零范围，丢弃原交易会话假设。 |
| R09 | [Dynamic ATR sizing][r09] | 日损益每根 bar 累加最后一笔已平仓收益，缺少新增交易检查；部分平仓 ID 与开仓 ID 不符；把至少 5 个价格单位当“滑点”加入价格，不能跨币价复用。原实现排除。 |
| R10 | [Multi-state Choppiness][r10] | 用两个端点的高低差先取 log，再放入 CI 的分母，不是全窗口极差；小价币可遇到非正对数输入；退出还将交叉事件当持续状态。原实现排除。 |
| R11 | [Momentum ADX][r11] | 名为 ShortEntry，却向旧版 entry 的 long 参数传 true；仍沿用 800/400 tick 参数。标题不能作为交易方向或币种适配依据。 |
| R12 | [Multi-factor Momentum Rotation][r12] | 只有单品种 RSI/TD/MACD/BB 条件，没有横截面排名或换仓组合；止损默认关闭。不是要寻找的多币轮动。 |
| R13 | [Dynamic Spread Market Making][r13] | 以均线构造阈值，命中后发市场方向单，没有双边 limit 报价；inventory 在信号时修改而非跟随成交。并非首页所称 A-S 做市实现，后置。 |
| R14 | [Adaptive Intelligent Grid][r14] | 源码与四份其他条目规范化后相同；多层叠仓、浮动网格及内存布尔库存，与单方向交易模型不同。不是当前目标优先项。 |
| R15 | [Risk-sized SuperTrend][r15] | 有权益风险除以止损距离的公式，但 floor 到整数、允许 500 次叠仓，组合止损与反转依赖模拟器。只参考公式，复用 V2 定仓。 |
| R16 | [High-frequency Intertemporal Arbitrage][r16] | 旧 kumex SDK，读本地明文 config，60 秒轮询，顺序发两腿订单；无单腿成交失败补偿，短仓平仓路径还复用带符号数量。不是高频生产引擎，不接入当前执行模型。 |
| R17 | [Dual EMA + ATR/ADX][r17] | 标成 ADX 的值实际是收盘价 RMA，再与 30 比较，指标带有价格单位；不能用于跨币强弱阈值。原实现排除。 |
| R18 | [Chandelier Exit Strategy][r18] | 与 R06 同类的动态退出公式，固定 25 资金单位的长仓演示；未写许可不证明公式来源已无授权义务。退出思想可研究。 |
| R19 | [Adaptive Risk + DMI/ADX][r19] | 使用 ta.dmi，EMA 方向/回撤与摆动止损逻辑可拆分；原始黄金标题与 BTC 回测注释混用，长 EMA 还需要足够真实高周期历史。作为 F 的相对清晰参考。 |
| R20 | [ZLSMA + Chandelier + Volume][r20] | 有相对成交量因子；将 swing low/high 放到 strategy.entry(stop=...) 是入场触发单，不是已持仓保护止损。只参考 RVOL，不照搬风险控制。 |

以上为静态代码推导，未声称在 TradingView 或 FMZ 编译执行。关于 entry/exit stop 的区别、tick 单位、匹配交易 ID 和模拟器成交语义，以 [TradingView 官方策略文档][pine-strategies] 为准。

## 6. 与现有 V2 的真实衔接点

本节依据代码，不以过时架构文档中的默认参数替代当前行为。

### 6.1 必须首先区分 K 线周期和窗口

`services/v2_s3_candles.py` 当前以 1,440 根已收盘 1m K 线构建 15m/1h/4h/24h **滚动分钟窗口**，并明确说明这不是交易所高周期 K 线。窗口中的 EMA20 不是“20 根 15 分钟 K 线的 EMA”。

新策略应增加有版本的 5m/15m/1h 等真实聚合序列，时间按交易所 bar 边界对齐，展示仍用 UTC+8；不能因展示时区而移位真实开收盘时间。一天 1m 历史只有 96 根 15m bar，不能直接满足 15m EMA200，更不能满足长日线通道；需要按策略登记 warm-up 要求并从已验证历史归档加载，不足时明确拒绝。

`services/v2_price_features.py` 当前刻意保留旧 ATR 采样：oldest→newest 数组前 14 个 TR 的算术均值；`s3/core.py` 的 RSI 也不是标准 Wilder 平滑。这与 Pine 常用 ATR/RSI 不可互换。近期精度修复解决了小币价舍入，不是指标定义标准化。应新增标准指标版本和对照样本，保留旧证据的可回放性，不能直接覆写旧指标含义。

### 6.2 保留执行链，扩展纯决策接口

已有 `v2_core/strategy.py` 支持纯 evaluator、冻结 config/context、持久化决策、幂等 intent 和证据；这些应复用。但 `services/v2_directional_admission.py` 与 `services/v2_trading_pipeline.py` 目前仍限定 S6/S8、s3/tv_bridge 及指定版本，不能仅添加一个类就声称全系统可插拔。

后续需要显式策略注册表和受控准入：strategy_id/version、指标版本、适用环境/账户、币种资格、周期、参数 schema、warm-up、信号寿命、计算预算、启停/回滚、退出策略、归因维度。先做 shadow，不让新模块自行使用交易所 API。

建议关系：共享行情和标准指标 → 独立候选策略/TV 候选 → 机会聚合与市场状态路由 → 统一账户风险 → 现有 intent/执行/保护/结算 → 版本化分析。聚合器不是所有策略必须同时同意；不同策略可独立产生候选，风险检查仍强制执行。

### 6.3 TradingView 应成为同一机会的证据，而非第二套订单

`services/v2_shared_market_context.py` 已提取 s3/tv_bridge 的同向/冲突证据，`StrategyWorker` 有同 consumer、同币种方向、跨 source 的时间窗口去重；这不等于任意多策略消费者之间已经有组合级去重和风险预算。

扩展后保留 strategy family、来源、标准周期、确认 bar 时间、信号版本；TV 可独立触发候选或补充同一机会，不必等待本地另发一次信号。相关的 EMA/MACD/Supertrend 不能算三份独立置信证据。去重需要区分同一机会的重复投递与新的合法交易周期，按账户/环境隔离；不同 producer 的预算和相反方向冲突也要统一仲裁。

### 6.4 存储、定仓、退出不要重造

PostgreSQL 保留业务真相、状态机和审计；Redis 是可重建特征/上下文缓存；ClickHouse 存行情与分析。第三方 `_G`、全局数组、config.json 不能变成并发业务状态或密钥来源。

`v2_core/directional.py:size_candidate` 已有剩余资金池、可用保证金、单笔保证金、止损风险与成本缓冲约束，再受波动率和回撤系数约束、按交易所 step 向下取整。H 应补标准波动输入和压力测试，不换回单一权益百分比，更不能用信号数量提升总保证金使用率。任何止损金额都只是估算，不保证跳空或流动性不足时不超损。

`v2_core/directional_exit.py` 已有低收益停滞、时间、部分止盈、峰值回撤等退出。G 是比较波动率自适应退出与这些基线；原生保护不应被软件指标替代。新增移动保护必须验证单向收紧、剩余数量、部分成交、撤换单故障和重启恢复，避免出现保护空窗。

### 6.5 资源与吞吐

不能给每个策略新建一套行情轮询。共享基础数据和同参数指标，按收盘事件增量计算，廉价全币资格检查与高成本候选计算分层。指标共享键须含 venue/product/symbol/timeframe/版本/参数/收盘时刻，不能混用现货和永续或不一致市场数据。

例如 500 币×8 模块为 4,000 个逻辑组合，每 15 分钟一次平均约 4.4 次/秒，但边界时刻可能同时触发，指标成本也不同；这是算术示例，不是本机压测结果。优先批处理、增量计算和持仓管理资源预留，再测 CPU/RSS、缓存命中、PG/CH IO、每轮时间、信号 age 的 P50/P95/P99。禁止用平均值宣称没有压力。

最近已记录的 V2 13.8—15.2 秒完整周期及短暂积压是既有文档证据，并非本轮测量；因此更不能立即把数千变体挂到同步消费路径。队列容量应由压测和信号失效期限决定，不降低风控或延长过期信号来追单。

## 7. QA 与收益验证必须分开

1. **许可和静态审查**：固定源码版本；剔除未来泄漏、方向/数量单位错误和名不副实逻辑；记录未解决授权。
2. **指标与信号一致性**：与独立公式/TradingView 已确认样本逐 bar 比对；单测 warm-up、平盘零分母、缺 bar、零成交量、小币价、时区边界；旧版本重放不改变。
3. **事件驱动回测**：只使用决策时已知的数据；收盘信号不能随意按同一根收盘价无摩擦成交；采用后续可成交价格、真实 tick/lot/notional、手续费、资金费、滑点和成交限制。历史币种资格按当时状态重建，避免幸存者偏差。
4. **样本外和消融**：固定训练/验证/留出集、滚动验证、跨币/牛熊震荡区间；记录搜索了多少变体、修正多重比较和过拟合风险。对比现有 V2、简单基线、仅 TV、仅本地、融合，不能只展示赢家。
5. **Shadow 与组合评估**：记录本该触发但未下单的候选与拒绝原因；检验同向相关性、资金占用、回撤和尾部风险；延迟到达的 TV 信号按真实接收时间处理，不按事后理想时间。
6. **Testnet 全链路验收**：自然或明确隔离测试信号 → 去重 → 风险预留 → 实际 ack/fill → 原生保护 → 减仓/平仓 → 手续费/资金费 → 结算与统计。增加重启、失联、重复通知、单腿/部分成交和账户切换故障测试。

每个阶段保存输入数据版本、参数、策略版本、测试命令和结果。静态通过≠编译通过；单测通过≠成交；Testnet 成交≠真实市场有收益。测试环境适合验工程行为，策略盈利证据还需要真实市场行情和成本建模，不能仅看测试币余额上涨。

评价不只看胜率：净期望、盈亏比、费用/资金费占毛利比例、最大回撤、最长持仓、保证金占用时长、MFE/MAE、捕获的趋势幅度及利润集中度。必须能解释“信号没有来 / 收到后拒绝 / 被预算压住 / 执行失败 / 盈利被回吐”，而不是再次以长时间不开单作为唯一结果。

## 8. 建议后续工作顺序

1. 完成本报告候选对应的许可核验与真实周期/标准指标版本方案。
2. 建研究回放和影子策略接口，保持正式交易参数与旧证据不变。
3. 首批 F/G/A/B，先单模块、再消融组合，按统一风险预算比较。
4. 达到样本外、运行时延和故障 QA 门槛才进入 Testnet 小范围启用；保留独立开关和回滚。
5. 后续研究 C/D/E/H；网格、马丁、做市、跨期/跨平台套利不混入本次方向性改造。

用户本次要求是评估，本报告不代表已授权或已开始上述实现/部署。

## 来源

[vault]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/tree/c9d6fa49486855899a92fea65004f440533049aa
[repainting]: https://www.tradingview.com/pine-script-docs/concepts/repainting/
[pine-strategies]: https://www.tradingview.com/pine-script-docs/concepts/strategies/
[r01]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Python-Version-Multi-Asset-Momentum-Strategy-Tutorial.md
[r02]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Digital-Currency-Futures-Multi-Variety-ATR-Strategy-Teaching.md
[r03]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Donchian-Channel-Breakout-Strategy-with-ATRSL-Trailing-Stop.md
[r04]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Momentum-Squeeze-Breakout-Trend-Tracking-Strategy.md
[r05]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/SuperTrend-Strategy-with-Trailing-Stop-Loss.md
[r06]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Chandelier-Exit.md
[r07]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/VWAP-Standard-Deviation-Mean-Reversion-Trading-Strategy.md
[r08]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Choppiness-K-line-Breakthrough-Strategy.md
[r09]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Multi-Indicator-Dynamic-Adaptive-Position-Sizing-with-ATR-Volatility-Strategy.md
[r10]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Adaptive-Multi-State-EMA-RSI-Momentum-Strategy-with-Choppiness-Index-Filter-System.md
[r11]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Momentum-Breakout-Strategy-with-ADX-Filter.md
[r12]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Multi-factor-Momentum-Rotation-Strategy.md
[r13]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Dynamic-Spread-Market-Making-Strategy.md
[r14]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Adaptive-Intelligent-Grid-Trading-Strategy.md
[r15]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Dynamic-Position-Sizing-SuperTrend-Trend-Following-Strategy-with-51-Reward-Risk-Ratio.md
[r16]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/High-Frequency-Intertemporal-Arbitrage-Strategy.md
[r17]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Dual-EMA-Crossover-Trend-Following-Strategy-with-ATR-and-ADX-Filters.md
[r18]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Chandelier-Exit-Strategy.md
[r19]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/Adaptive-Risk-Strategy-with-Dual-EMA-Crossover-and-ADX-Momentum-Filter.md
[r20]: https://github.com/brainbrick-trades/The-Quant-Trading-Vault/blob/c9d6fa49486855899a92fea65004f440533049aa/strategies/ZLSMA-Enhanced-Chandelier-Exit-Strategy-with-Volume-Spike-Detection.md
