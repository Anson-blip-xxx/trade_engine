# Decision / Risk Inventory（Phase 3-00 · READ-ONLY）

> 基于 feature/v2-architecture @ `79e8958`（P2 fail-open 修复后）的实际代码逐行核实。
> 本阶段只记录现状，不实现、不重构、不修改任何生产逻辑。
> 所有行号以当前 HEAD 为准，后续改动请以函数名为锚。

---

## 1. 当前交易链路（含实际代码位置）

```
S3 detect_events (s3_orderflow.py:388-560)          TV tv_bridge (:8001)
  → Redis event:s3                                    → Redis event:tv
  └──────────────┬──────────────────────────────────────┘
                 ▼
   read_all_signals() (shared_executor.py:259)
                 ▼
   S6._open_long (S6.py:121)      S8._open_short (S8.py:89)
     ├─ Unified Signal (signals/adapters.py，旁路)
     ├─ DecisionJournalBuilder (旁路，fail-open)
     ├─ Gates（见 §2 Decision Inventory，S6.py:130-265 / S8.py:96-246）
     ├─ Score: contract_score (S6.py:274 / S8.py:256)
     ├─ Risk:  leverage_for_score + bounded_stop_pct + calc_position_qty
     │         (S6.py:276-284 / S8.py:258-266)
     ▼
   open_position (shared_executor.py:1052-1247)
     ├─ 内部 Gates（strength30/analysis/R:R/熔断/4h冷却/PAUSE/费率/交易所查仓）
     ├─ Execution（杠杆/保证金/MARKET 单/成交解析）
     ├─ PM 注册 (_update_pos_cache → pm:positions)
     ├─ Algo SL 入队 + PG event + Telegram
     ▼
   Position Manager (position_manager.py，P1-04 已冻结)
     └─ _monitor_one 11 步退出链 → _close → trade_recorder → PG/CH
```

**注意**：链路中存在**两段决策**——S6/S8 的 Gate 链（含 S6.py 内的 Strategy 层过滤）与
`open_position()` 内部的第二段 Gate 链。两段共同构成完整 Decision，当前耦合在三个文件里。

---

## 2. Decision Inventory

| function | file:line | caller | current responsibility | proposed V2 layer | pure/IO | risk level |
|---|---|---|---|---|---|---|
| `long_signal_allows_open` | shared_executor.py:812 | S6._open_long:130 | VIOLENT_BULLISH × weak_bear/risk-off regime 拒绝 | Decision/Gate | **pure** | LOW |
| `short_signal_allows_open` | shared_executor.py:806 | S8._open_short:94 | TREND_DOWN/VIOLENT_BEARISH/PULSE_DOWN 强度 ≥60 门槛 | Decision/Gate | **pure** | LOW |
| `event_is_stale` | shared_executor.py:661 | S6:137 / S8:~105 | 事件 120s 过期判定 | Decision/Gate | pure | LOW |
| `is_event_fresh` | shared_executor.py:289 | S6:145 / S8:~112 | (symbol,event_type) 180s 重复冷却（内存 `_event_history`） | Decision/Gate | **进程内状态** | MEDIUM |
| `event_age_sec` | shared_executor.py:669 | S6/S8 score 输入 | 事件年龄（score 罚分输入） | Decision 输入 | pure*（读 evt ts） | LOW |
| `event_is_stale` 内 `since>10**12` ms 判断 | 同上 | — | 时间单位嗅探 | Decision | pure | LOW |
| `maybe_replace_recovery_position` | shared_executor.py:629 | S6:155 / S8:~118 | 恢复模式 1 仓上限 + 候选评分替换（**会平掉现有仓**） | Decision/Gate | **IO（close_position）** | **HIGH** |
| `market_allows_trading` | shared_executor.py:1276 | S6:178 / S8:~140 | S0 market_mode==risk_off 全局拦截 | Decision/Gate | Redis 读 | LOW |
| `get_position_count` | shared_executor.py:392 | S6:151 / S8:~114 | 当前策略持仓数（PM 缓存） | Decision 输入 | 内存缓存/PM load | MEDIUM |
| `has_any_position` | shared_executor.py:414 | S6:186 / S8:~149 | 同 symbol 已有仓检查（缓存+positionRisk 兜底） | Decision/Gate | **IO 兜底** | MEDIUM |
| `classify_entry_mode` | shared_executor.py:735 | S6:203 / S8:~170 | LEFT_REVERSAL / RIGHT_MOMENTUM / UNCONFIRMED 分类 | Decision/Gate | **pure** | LOW |
| `long_trend_takeover_ready` | shared_executor.py:751 | S6:~228（takeover 分支） | S6B 趋势接管条件 | Decision/Gate | pure（读 market dict） | LOW |
| `pump_down_uptrend_guard` | shared_executor.py:771 | S8:~194（PUMP_DOWN only） | PUMP_DOWN 高周期逆势保护 | Decision/Gate | pure | LOW |
| `price_is_overextended` | shared_executor.py:676 | S6:253 / S8:235 | 距 1h EMA20 超过 max_atr 拒绝追价 | Decision/Gate | **pure** | LOW |
| `get_short_ratio` | shared_executor.py:685 | S6:204 / S8:~171 | Binance 全局多空账户比（score 输入，5min 缓存） | Decision 输入 | **网络**（缓存） | MEDIUM |
| `resolve_event_flow` / `resolve_event_orderflow_bias` | shared_executor.py:708/725 | S6:205-206 / S8:172-173 | 事件流字段优先、回退 S3 快照 | Signal→Decision 输入 | pure | LOW |
| `contract_score` | shared_executor.py:779 | S6:274 / S8:256 | 0-100 综合评分（strength ±flow ±short_ratio −ATR −extension −age） | **Decision/Score** | **pure** | **HIGH（核心）** |
| strength≥30 基础门槛 | shared_executor.py:1059 | open_position | 信号最低强度 | Decision/Gate | pure | LOW |
| `_analysis_allows_open` | shared_executor.py:904 | `_analysis_gate`:1022 | 同(symbol,event,system) 14 天滚动胜率/质量/T60 过滤 | Decision/Gate（自适应） | **CH 读 + Redis 缓存** | MEDIUM |
| `_analysis_gate` | shared_executor.py:1022 | open_position:1063 | 硬拒/软降权（ANALYSIS_FILTER_MODE） | Decision/Gate | CH+Redis | MEDIUM |
| R:R 预判 | shared_executor.py:1069-1074 (`_MIN_RR`:876) | open_position | 预期延续/止损距离 ≥1.0 | Decision/Gate | pure | LOW |
| funding 极值 gate | shared_executor.py:1104-1109 (`_get_funding_rate`:76) | open_position | SHORT 费率<-0.1% / LONG>+0.1% 跳过 | Decision/Gate | **网络** | MEDIUM |
| 交易所已有仓位 gate | shared_executor.py:1114-1119 | open_position | BOTH 模式反向净仓拦截 | Decision/Gate | **网络** | MEDIUM |
| 4h 重开冷却 | shared_executor.py:1085 (`_was_closed_recently`:1040) | open_position | 平仓后 4h 不重开 | Decision/Gate | Redis | LOW |
| `event_is_stale`（S3 事件层，同上） | — | — | — | — | — | — |
| `bounded_stop_pct` | shared_executor.py:103 | S6:282 / S8:264 | 止损距离 = max(固定, ATR×2) 且 ≤ 上限 | **Risk**（但决定 R:R 输入） | pure | MEDIUM |
| `_get_funding_rate` | shared_executor.py:76 | funding gate | 费率查询（缓存 60s） | Decision 输入 | 网络（缓存） | LOW |

### S6/S8 内联 Gate（策略文件内，非 shared_executor）

| Gate | S6 位置 | S8 位置 | 差异 |
|---|---|---|---|
| regime | S6.py:130 | —（S8 无此 gate） | S6 对 VIOLENT_BULLISH 做 regime 门控；S8 无对应 |
| strength | — | S8.py:94 | S8 有 raw_strength≥60 门槛；S6 无 |
| fresh | S6.py:137 | S8.py:~105 | 相同（event_is_stale） |
| signal_cooldown | S6.py:145 | S8.py:~112 | 相同（180s） |
| recovery_replace | S6.py:153-159 | S8.py:~118-124 | 相同（drawdown_mode 恢复模式） |
| position_limit | S6.py:161（MAX=2） | S8.py:~130（MAX=2） | 相同 |
| symbol_cooldown | S6.py:170 | S8.py:~139 | 相同（state cooldowns 2h/4h） |
| market_allowed | S6.py:178 | S8.py:~148 | 相同（S0 risk_off） |
| existing_position | S6.py:186 | S8.py:~157 | 相同 |
| price | S6.py:195 | S8.py:~166 | 相同 |
| pump_guard | — | S8.py:194-200（PUMP_DOWN only） | S8 独有 |
| entry_mode | S6.py:~215（UNCONFIRMED 拒绝） | S8.py:218 | 相同语义；S6 多 S6B takeover 分支 |
| takeover | S6.py:225-235（PUMP_UP/VIOLENT_BULLISH+4h/24h） | — | S6 独有（S6B 路径） |
| trend | S6.py:246（price<ema20 拒绝） | S8.py:225（price>ema20 拒绝） | 方向镜像 |
| extension | S6.py:252（max 1.25 VIOLENT_BULLISH） | S8.py:235（max 1.25 VIOLENT_BEARISH） | 镜像 |
| atr | S6.py:261（takeover 上限 12） | S8.py:244（上限 6） | S6B takeover 更宽 |

---

## 3. Risk Inventory

| function | file:line | caller | current responsibility | proposed V2 layer | pure/IO | risk level |
|---|---|---|---|---|---|---|
| `calc_position_qty` | shared_executor.py:835 | S6:284 / S8:~259 | 仓位数量总计算（池化→分配→ATR 衰减→1% 风险帽） | **Risk** | **IO（余额 API + PM load）** | **HIGH（核心）** |
| `score_to_fraction` | shared_executor.py:504 | calc_position_qty:510 | score→池占比（3%~15% 线性） | Risk | pure | LOW |
| `AtrRiskPositionSizer` | position_models.py:6-26 | calc_position_qty:852 | 纯数学仓位模型（pool/分数/ATR 衰减/风险帽/min notional） | Risk | **pure**（整个文件唯一纯类） | LOW |
| `_POOL_BUDGET/_POSITION_MIN_PCT/_POSITION_MAX_PCT/_POSITION_MIN_USDT/_RISK_PER_TRADE` | shared_executor.py:497-501 | calc_position_qty | 风险参数常量 | Risk config | 常量 | MEDIUM（改=改行为） |
| `bounded_stop_pct` | shared_executor.py:103 | S6:282 / S8:264 | stop = max(固定, 2×ATR%) capped | Risk | pure | MEDIUM |
| `STOP_LOSS_PCT` / `MAX_STOP_LOSS_PCT` / `LEVERAGE` / `MARGIN_MODE` / `MAX_ATR_PCT` / `MAX_POSITIONS` | S6.py:44-64 / S8.py:46-67 | S6/S8 开仓参数 | 每策略风险参数 | Risk config | 常量 | MEDIUM |
| `leverage_for_score` | shared_executor.py:820 | S6:276 / S8:~257 | score/atr→杠杆（5/3/2 档） | Risk | pure | LOW |
| `_drawdown_status` | shared_executor.py:560 | open_position:1076、drawdown_mode、S6/S8 recovery 分支 | 账户回撤状态机（peak/halt/recovery/loss_lock） | **Risk（账户级）** | **IO（余额 + Redis peak/pause）** | **HIGH** |
| `drawdown_mode` | shared_executor.py:617 | S6:154 / S8:~117 | normal/reduced/recovery/halt | Risk | 同上 | HIGH |
| `_DD_*` 常量（8%/15%/4h/0.25/1仓/2%/6h） | shared_executor.py:552-558 | _drawdown_status | 回撤熔断参数 | Risk config | 常量 | MEDIUM |
| `maybe_replace_recovery_position` | shared_executor.py:629 | S6/S8 recovery 分支 | 恢复模式候选替换（含平仓动作） | Risk（含 Execution 动作！） | IO | **HIGH** |
| 1% 风险帽（风险约束步骤） | position_models.py:23-25 + calc_position_qty:860-862 | — | stop_pct×leverage×qty ≤ balance×1% | Risk | pure | LOW |
| min notional | shared_executor.py:84 (`_get_min_notional`) + :1097 + position_models.py:11 | open_position / sizer | 最小名义价值 | Risk | 网络+常量 | LOW |
| pool budget（80% 池化） | shared_executor.py:497,529-535 | calc_position_qty | 余额×80% − 已用保证金 | Risk | IO | MEDIUM |
| S6B takeover 风险参数 | S6.py:276-282（leverage=2 / stop 0.10→0.12 cap / ATR 12） | — | 趋势接管更宽风险 | Risk config | 常量 | LOW |

**Risk 现状要点**：
1. `calc_position_qty` 是 Risk 核心，但它 **混合了 IO**（`_get_balance` 实时 API、
   `_calc_used_margin` 实时 API）与纯计算（sizer.budget）。
2. `AtrRiskPositionSizer`（position_models.py）是当前唯一干净的纯 Risk 类，
   但 `calc_position_qty` 同时还重复了一份 sizer 外的池化逻辑——**两套池化计算并存**
   （公式重复：`remaining × alloc_pct` 在两处出现）。
3. 回撤状态机（`_drawdown_status`）横跨 Risk（熔断）与 Decision（recovery 分支 gate）。

---

## 4. Execution Inventory（open_position 解剖）

`shared_executor.open_position`（:1052-1247）——**Decision/Risk 只占前 ~70 行，
其余全部是 Execution**：

| 步骤 | 行号 | 类别 |
|---|---|---|
| strength≥30 / analysis gate / R:R / 熔断 / 4h 冷却 / PAUSE / min notional / funding / 交易所查仓 | 1059-1119 | Decision+Risk（见 §2/§3） |
| `_round_qty`（exchangeInfo LOT_SIZE） | :1133 | Execution |
| 杠杆 API `POST /fapi/v1/leverage`（PM._s6api 或自带 fapi_post） | PM:781-785（PM 版）；shared_executor 版在 MARKET 前后 | Execution |
| 保证金模式 `POST /fapi/v1/marginType` | 同上 | Execution |
| **MARKET 单** `POST /fapi/v1/order`（reduceOnly=BOTH） | shared_executor.py:~1145-1150 | Execution |
| 成交解析（avg_price / executedQty / 部分成交撤单重试 / 状态 NEW-FILLED） | :~1155-1185 | Execution |
| PM 注册 `_update_pos_cache`（写 `_POS_CACHE` → `pm:positions`） | :1186-1187 | Execution+PM 桥 |
| Algo SL 入队（`_algo_enqueue` via PM） | PM:806-808 | Execution |
| PG event `OPEN_ORDER_FILLED`（含 decision_context） | shared_executor.py:1127 | Persistence |
| Telegram 通知 | :1142-1148 | Notification |
| Sandbox 拦截（binance_api + shared_executor 双拦截点） | :169-181 | Infra |

**结论**：`open_position()` 不属于 Phase 3 Decision/Risk。它是一个
Decision-Gates + Risk-Checks + Full-Execution 的复合体，
应整体留到 **Phase 4 Execution Extraction**，届时只把前 70 行的
Gates/Checks 抽为可注入的 Decision/Risk 评估函数，单据部分原样保留。

---

## 5. Cross-layer contamination（混合职责清单）

| 位置 | 混合的层 | 细节 |
|---|---|---|
| `S6._open_long` / `S8._open_short` | **Strategy + Decision + Risk + Execution 调用** | 一个函数 180+ 行：Signal 消化→17 道 Gate→score→仓位→止损→下单调用。Strategy（事件类型→system_tag/参数表）与 Decision（gates）与 Risk（sizing 调用）无边界 |
| `shared_executor.open_position` | **Decision + Risk + Execution + PM + Persistence + Notification** | 见 §4；最严重的 God Function |
| `calc_position_qty` | **Risk + IO** | 纯 sizer 数学混入实时余额 API/PM load/日志 |
| `_drawdown_status` | **Risk + 状态持久化 + Decision 语义** | 回撤状态机读写 Redis `account:peak/dd_pause`，输出同时被 Risk（×factor）与 Strategy（recovery 分支 gate）消费 |
| `_analysis_gate` / `_analysis_allows_open` | **Decision + Persistence + 自适应学习** | 读 ClickHouse 历史 + Redis 缓存，输出 hard 拒单/soft 降权——决策逻辑依赖分析存储 |
| `maybe_replace_recovery_position` | **Risk + Execution** | Risk 评估内部直接调用 `close_position` 平掉现有仓 |
| `_get_balance` / `_calc_used_margin` | **Risk 输入 + IO** | Risk 计算强依赖实时账户 API |
| `is_event_fresh`（内存 `_event_history`） | **Decision + 进程状态** | 冷却状态只在单进程内存（S6/S8 各一份），非跨进程 |
| `classify_entry_mode` 与 S6 takeover 分支 | **Decision（部分在策略内联）** | entry_mode 分类是纯函数，但 S6B takeover 分支逻辑内联在 S6 |

---

## 6. S6 / S8 对称性（以实际代码为准）

### 相同逻辑（完全对称）
- fresh / signal_cooldown / recovery_replace / position_limit / symbol_cooldown /
  market_allowed / existing_position / price / entry_mode / extension / atr 的
  **结构**完全一致
- score 输入（contract_score 同一函数，参数镜像）
- 资金费率极值 gate（open_position 内，方向镜像）
- 交易所查仓 gate（BOTH 模式）
- journal 旁路（fail-open 链路一致）

### 方向差异（镜像）
- trend：S6 拒 `price < ema20`；S8 拒 `price > ema20`
- extension 输入符号：`(price-ema20)/atr` vs `(ema20-price)/atr`

### 真实差异（**不是**简单镜像）
| 维度 | S6 | S8 |
|---|---|---|
| regime 门控 | **有**（VIOLENT_BULLISH × weak_bear/risk-off 拒绝） | **无** |
| strength 门槛 | **无**（仅 open_position 内 strength≥30） | **有**（TREND_DOWN/VIOLENT_BEARISH/PULSE_DOWN raw≥60） |
| takeover 分支 | **有**（S6B：PUMP_UP 或 VIOLENT_BULLISH+4h>3+24h>10 或 breakout_confirmed；leverage=2、stop 0.10→0.12、ATR 上限 12、`long_trend_takeover_ready` 确认） | **无** |
| pump_guard | **无** | **有**（PUMP_DOWN × 高周期上行拒绝） |
| VIOLENT extension | 1.25（VIOLENT_BULLISH） | 1.25（VIOLENT_BEARISH） |
| 止损基线 | PULSE_UP 0.04 / 其余 0.06 | PULSE_DOWN 0.04 / PANIC_SELL 0.035 / 其余 0.05 |
| S3 事件→策略映射 | PULSE_UP/TREND_UP/VIOLENT_BULLISH→S6A，PUMP_UP→S6B | 全部→S8（含 PANIC_SELL） |
| recovery 替换日志 | 带 symbol 名 | 无 symbol 名 |

**结论**：S6/S8 是"80% 镜像 + 20% 真实差异"。V2 抽取 Decision 时必须把
这 20% 差异（regime gate / strength gate / takeover / pump_guard / 止损基线）
显式参数化，而不是假设对称。

---

## 7. Proposed Decision Context（只设计，不实现）

Decision 评估需要的完整输入快照。逐字段标注现状：

| 字段 | 现状 | 来源 |
|---|---|---|
| Unified Signal | ✅ 已存在（Phase 2 `signals.Signal`） | signals/ |
| Market snapshot（price/ema20/atr/atr_pct/rsi/flow） | ✅ 已存在（S6/S8 从 `market:s3_data` 读取的局部变量；Journal MarketSnapshot 已建模） | 现场变量，未对象化 |
| Regime snapshot | ✅ 已存在（S6 读 `get_market_state()`；S8 **未读**——仅经 market_allows_trading 间接判断；Journal RegimeSnapshot 已建模） | S6 局部变量 |
| Position snapshot（同 symbol 持仓数/已有仓） | ✅ 部分存在（`get_position_count`/`has_any_position` 标量；无对象化快照） | PM 缓存 |
| Account/risk snapshot（balance/used/pool/dd state） | ❌ **不存在对象化快照**——散落在 `_get_balance()`、`_calc_used_margin()`、`_drawdown_status()` 多次独立 IO | 需 Phase 3 组装 |
| Event metadata（age/fresh） | ✅ 已存在（`event_age_sec`/`is_event_fresh`） | 内存 |

**Phase 3-02 需要新增的组装**：Account/Risk Snapshot 的对象化（把三次独立 IO
合并为一次快照读取），其余全部现成。**禁止新增数据采集**（不加快照刷新频率、
不加新 API 调用——只是把现有读数组装为对象）。

## 8. Proposed Decision（最小模型）

```
Decision:
  action        : OPEN / REJECT（Phase 3 范围；HOLD/CLOSE 留 PM 退出链）
  accepted      : bool
  reason        : str | None        # 与 Journal GateResult.reason 同源
  score         : int               # contract_score 输出
  entry_mode    : str | None
  gates         : tuple[GateResult] # 直接复用 journal.GateResult，不新造
```

**明确不负责**（这些属于 RiskDecision / Execution / PM）：
- quantity、leverage、margin_mode、stop_pct、stop_price
- 订单、下单参数
- PM 持仓生命周期

与 Journal 的关系：Decision 的 gates/decision 字段与
`journal.models.GateResult/DecisionResult` **同构**（P1-02 已经在记录它们）——
Decision 对象就是 Journal 已记录内容的内存形态，不引入第二套表达。

## 9. Proposed RiskDecision（最小模型）

```
RiskDecision:
  accepted       : bool              # False = qty=0 / 熔断 / 超限
  quantity       : float             # calc_position_qty 输出
  leverage       : int               # leverage_for_score 输出
  margin_mode    : str               # ISOLATED/CROSSED
  stop_pct       : float             # bounded_stop_pct 输出
  stop_price     : float             # entry ±(1∓stop_pct)
  reason         : str | None        # 熔断/超限原因
```

输入：Decision + DecisionContext（market/regime/account snapshot）。
**明确不负责**：下单、PM、SL 单挂载、费率 gate（费率 gate 语义是 Decision：
"这个标的现在不适合交易"，不是"承担多少风险"——Phase 3 归 Decision/Gate）。

## 10. Extraction order（严格推荐）

```
Phase 3-01  Decision characterization tests
            对 contract_score / classify_entry_mode / leverage_for_score /
            bounded_stop_pct / price_is_overextended / score_to_fraction /
            short_signal_allows_open / long_signal_allows_open 逐一建
            纯函数黄金测试（全部 pure，零风险）。
            为什么先做：这些都是 pure 函数，测试即文档；且它们是后续
            拆分的全部"行为锚点"。
            ⚠ 旧行为是 source of truth——发现"不合理"只记录不修复
            （参照 P1-04 PM_GOLDEN_OBSERVATIONS.md 的格式）。

Phase 3-02  Decision extraction
            把 S6/S8 Gate 链 + shared_executor 纯 Decision 函数抽为
            decision/ 包（保留 re-export shim，行为零变化），S6/S8 改为
            调用 Decision.evaluate(context) → Decision。
            验收：Journal 产出逐字段一致（用 P1-03 Replay diff + P1-02
            integration 测试双保险）。

Phase 3-03  Risk characterization tests
            对 calc_position_qty / score_to_fraction / AtrRiskPositionSizer /
            _drawdown_status / drawdown_mode / maybe_replace_recovery_position
            建测试。⚠ calc_position_qty 含 IO（balance/margin API）——
            characterization 必须以 P1-04 同款 fixture 隔离 IO 后冻结。
            maybe_replace_recovery_position 含 close_position 副作用——
            只冻结判定部分，动作部分标注到 Phase 4。

Phase 3-04  Risk extraction
            抽 risk/ 包：Risk.evaluate(decision, context) → RiskDecision。
            _drawdown_status 的 Redis 状态读写留原位（Phase 7 再抽 storage），
            先抽纯计算部分。验收同 3-02。

Phase 4     Execution extraction
            open_position 单据部分（杠杆/保证金/MARKET/成交解析/重试）→
            execution/ 包。Decision/Risk 已在 3-02/3-04 抽出，
            open_position 届时只剩"评估结果 + 执行单据"的薄壳。
```

**排序理由**：
1. 纯函数（contract_score 等）测试成本最低、回归风险为零 → 先冻结；
2. Decision 在 Risk 之前——因为 Risk 的输入（score/entry_mode）是 Decision 的
   输出，先定 Decision 边界，Risk 边界自然清晰；
3. open_position 的单据部分最后动——它直接碰 Binance，且 P1-04 已证明其
   周边（PM）敏感度最高；
4. 每步都有 Replay diff（P1-03）+ integration（P1-02）双保险。

---

## 11. Characterization Test 必建清单（Phase 3-01 输入）

| 函数 | file:line | 纯度 | 现有测试 | 缺口 |
|---|---|---|---|---|
| `contract_score` | shared_executor.py:779 | pure | ❌ 无 | **必须全建**：flow 对齐 ±5 / short_ratio ±8-5 / ATR 罚分 / extension 罚分 / age 罚分 / clamp 0-100 |
| `classify_entry_mode` | :735 | pure | ❌ 无 | LEFT/RIGHT/UNCONFIRMED 三分支 × LONG/SHORT |
| `leverage_for_score` | :820 | pure | ❌ 无 | 5/3/2 档位边界（score<60/<85/atr≥4） |
| `bounded_stop_pct` | :103 | pure | ✅ 部分有（3 例） | 补边界：atr=0、atr=4、base>cap |
| `price_is_overextended` | :676 | pure | ❌ 无 | LONG/SHORT × 边界 ATR 倍数 |
| `score_to_fraction` | :504 | pure | ❌ 无 | 3%~15% clamp 边界 |
| `AtrRiskPositionSizer.budget` | position_models.py:17 | pure | ❌ 无 | ATR 衰减因子、风险帽、min_notional 下限 |
| `short_signal_allows_open` / `long_signal_allows_open` | :806/:812 | pure | ✅ 部分（signals 测试） | 补全 60 门槛边界 |
| `event_is_stale` / `event_age_sec` | :661/:669 | pure* | ❌ 无 | 120s 边界、ms/epoch 嗅探 |
| S6 decision path | S6.py:121-315 | 混合 | ✅ P1-02 integration（Journal 侧） | 补**决策结果**侧（gate 序列 × 参数组合矩阵） |
| S8 decision path | S8.py:89-279 | 混合 | ✅ P1-02 integration | 同上 |

---

## 12. 架构边界（Phase 3 定义）

| 层 | 问题 |
|---|---|
| **Signal** | "发现了什么机会" |
| **Decision** | "现在要不要交易"（含哪些 gate 拦截了它） |
| **Risk** | "如果交易，承担多少风险"（仓位/杠杆/止损距离/熔断） |
| **Execution** | "如何向 Binance 下单"（单据/重试/成交解析） |
| **Position（PM）** | "持仓生命周期如何管理" |
| Journal | "以上全部过程的事实记录"（已建成，Phase 3 只消费不修改） |

**PM**：P1-04 已冻结，不属于 Phase 3。S7：KEEP/DEFER，不参与 Phase 3。

---

## 13. 高风险点汇总（供 Phase 3-01/02 规避）

| # | 风险 | 影响 |
|---|---|---|
| 1 | `contract_score` 无任何测试——它是 Decision 的核心数值，5 个输入项 7 种调整 | Phase 3-02 重构时若 score 漂移，所有 gate 结果全变 |
| 2 | `calc_position_qty` 混合 IO——characterization 必须先隔离 | 直接决定每单仓位大小 |
| 3 | `maybe_replace_recovery_position` 在 Risk 评估里执行平仓 | 抽取时误留副作用会造成双平仓 |
| 4 | S6/S8 的 20% 真实差异（regime/strength/takeover/pump_guard） | 假设对称合并会丢失行为 |
| 5 | 双套池化计算（calc_position_qty 池化 vs sizer.budget 池化公式重复） | 抽取时只迁移一套会漏掉另一套的效果 |
| 6 | `is_event_fresh` 进程内存状态 | 抽到 decision/ 后 S6/S8 仍各自持有独立状态（现状不变，但必须显式保留） |
| 7 | open_position 前段 gates 与 S6/S8 gates 语义重叠（如 strength30 vs strength60、冷却 vs 4h 重开） | 拆分时容易"合并同类项"而改变行为——禁止合并，只平移 |
