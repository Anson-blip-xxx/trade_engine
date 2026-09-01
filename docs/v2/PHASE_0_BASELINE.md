# Phase 0 - V2 Baseline Audit

> 审计日期: 2026-09-01
> 审计原则: 只读审计，未修改任何交易业务逻辑代码。本文件是 Phase 0 唯一新增产物。
> 运行状态: PAUSE_OPEN 激活（全局暂停开仓），持仓为零，PM/交易所无残留仓位。

---

## 1. Git Baseline

| 项 | 值 |
|---|---|
| Branch | `main` |
| HEAD | `cfb7839` docs: reflect current architecture (sentiment overlay, env tag, service list) |
| Remote | `origin git@github.com:Anson-blip-xxx/trade_engine.git` (fetch/push) |
| 未提交修改 | 仅 `?? strategies/config/`（未跟踪目录：PAUSE_OPEN 暂停开关 + S3/S6/S8 运行时状态 JSON）。**未做任何 reset/checkout/stash/delete** |
| 最近 5 commit | `cfb7839` README 架构更新 / `132c77e` auto: daily push 08-27 / `7bf9edb` TV flow passthrough + symbol-pool alignment / `1d560f8` auto: daily push 08-20 / `71ecc78` real-time profit-lock SL + README overhaul |

**建议**: 创建专用分支 `feature/v2-architecture`（本阶段未创建、未切换）。

**其他基线事实**:
- 无 `requirements.txt` / `pyproject.toml` / `.env.example`，仅有 `requirements-postgres.txt`（68B）。依赖管理不正式，V2 需补。
- `.gitignore` 存在；`strategies/config/` 内含运行时状态文件（s3_events.json、S6_state.json 等）与 git 目录混放。
- 存在 3 份 indicators（`/indicators.py`、`shared/indicators.py`（stub）、`services/s0/indicators.py`）。

---

## 2. Current Runtime Architecture

以实际代码与 systemd 运行时为准（README/SYSTEM_TOPOLOGY 与实际不一致处以代码为准）。

### 2.1 systemd 实际运行清单

| Service | 进程 | 状态 |
|---|---|---|
| trade-s0 | services/s0/s0_market_guard.py | running |
| trade-s3 | strategies/s3_orderflow.py | running |
| trade-s6 | strategies/S6.py | running |
| trade-s8 | strategies/S8.py | running |
| trade-sentiment | services/sentiment_bridge.py | running |
| trade-tv | services/tv_bridge.py | running（闲置，无付费 TV 套餐，无真实告警流入） |
| **S7** | — | **未运行（无 service 单元、无进程）** |

### 2.2 主链路（S3 信号路径，当前唯一活跃信号源）

```
Binance(demo-fapi) 
  → S3(s3_orderflow.py 单进程):
      get_top_symbols(24h quoteVolume top60, >MIN_VOL_24H)
      → WS 1m kline(60币) + REST fallback + @trade 大单WS(仅BTCUSDT)
      → 指标(EMA/RSI/ATR/量/taker_buy) 4窗口(15m/1h/4h/24h)
      → 事件检测(PULSE/TREND/PANIC/VIOLENT/PUMP/FAILED_BREAKOUT...) + 生命周期(active/END)
      → Redis: event:s3 {ts,events[]} / market:s3_data {ts,symbols{}}
      → pubsub s3:event:notify + JSON 文件兜底(strategies/config/s3_*.json)
  → S6/S8 主循环(10s 轮询 + notify 唤醒):
      read_all_signals() = read_s3_events() + read_tv_signals()(恒空)
      read_s3_market_data() → EMA20/ATR/flow
      Gate 链(见 §6/§7) → contract_score → calc_position_qty(1%风险帽)
      → open_position(): 币安市价单 + AlgoSL 入队 + PM 元数据写 pm:positions
      → 每循环 pm_monitor() → PM.monitor_all(system_filter) 管理持仓
  → PM(position_manager.py, 运行于 S6/S8 进程内):
      3层持仓同步(WS用户流[Redis选主] → REST positionRisk → pm:positions元数据)
      11 步退出链(§10) → _close() → trade_recorder.record_trade()
  → trade_recorder: PG trade_episodes upsert + CH trade_history insert
      + enqueue_closed_trade → trade_analyzer(T0/T15/T60 → trade_analysis)
      + Telegram 通知
  → S0(独立30s循环): sample_btc/breadth/alts_sync/shock_score/sentiment
      → market:s0{regime, regime_score, sentiment_risk, s6/s7/s8_allowed...}
      + market_state.json 文件 + CH market_state_log
```

### 2.3 旁路

```
TradingView ──webhook──► tv_bridge(:8001) ──► event:tv ──► S6/S8 read_all_signals()
  （secret校验→映射→规范化→币池对齐→去重→快照；当前闲置：TV Basic 套餐无 webhook）

alternative.me F&G + 真盘fapi funding ──► sentiment_bridge ──► market:sentiment
  ──► S0 compute_state 情绪叠加(sentiment_risk → regime_score-1) ──► market:s0
```

### 2.4 S7 完整链路（当前休眠）

```
s7_runner.main(): 300s 循环
  ├─ s7_core: 自带 fapi_get/post/delete + sign（独立 Binance API 层，不用 shared/binance_api）
  │            MarketDataFeed(WS) + get_price/imbalance/klines/atr/ema/balance/inventory
  ├─ s7_logic.manage_grid(symbol, runtime_state)   # 884 行网格逻辑
  ├─ s0_reader: 读 market:s0
  ├─ 30min 持久化 RuntimeState；importlib.reload(s7_logic) 热更新
  └─ config/grid.env + 独立文档
```

### 2.5 与文档不一致处（以代码为准）

- SYSTEM_TOPOLOGY.md 称 S3 写 `s3_events.json`/`s3_market_data.json` 为主，实际 Redis `event:s3`/`market:s3_data` 为主、JSON 为兜底。
- SYSTEM_TOPOLOGY.md 未提及 tv_bridge / sentiment_bridge / trade-tv / trade-sentiment。
- README 所述与代码一致（本阶段上一轮已校准过）。

---

## 3. Module Dependency Graph

实际 import 关系（grep 验证）：

```
S6.py ──┐
        ├─► shared_executor（注意: 经 sys.path 注入后以顶层名 `shared_executor` 导入，
S8.py ──┘      非 strategies.shared_executor —— 与测试的导入身份不同，存在双身份隐患）
              ├─► shared.position_manager (monitor_all/close_position/_algo_*/_load)
              ├─► shared.position_score
              ├─► shared.redis_store (_rget/_rset/_rsubscribe)
              ├─► scripts.sandbox (拦截)
              ├─► shared.trade_analyzer (get_rollup_stats, 惰性)
              └─ 自带 fapi 签名实现(_fapi_sig/fapi_get/fapi_post)  ← 独立 API 层

s3_orderflow ──► shared.redis_store, shared.binance_api(FAPI/FSTREAM)   [自洽]

s0_market_guard ──► shared.redis_store, shared.binance_api(FAPI), dotenv  [自洽]

position_manager ──► shared.redis_store(+lock), shared.binance_api(TG_TOKEN/CHAT_ID)
                     shared.postgres_client(record_trade_event), shared.exit_factors
                     shared.data_cache(klines), trade_recorder.record_trade(经 _s6api 惰性)
                     + 自带 _light_fapi_* 三件套                        ← 又一独立 API 层

trade_recorder ──► binance_api, redis_store, clickhouse_client, postgres_client,
                   trade_analyzer(enqueue), [惰性] s6_auto_trader(load_state/batch_get_prices/get_cycle_pnl/log)

trade_analyzer ──► binance_api(FAPI), clickhouse_client
tv_bridge ──► redis_store, binance_api.load_config
sentiment_bridge ──► redis_store（+ 独立 requests 公网调用）
s7_core ──► redis_store（+ 自带完整 fapi 层）    s7_runner ──► s7_core, s7_logic
s6_auto_trader ──► [LEGACY] 几乎无活引用（仅 trade_recorder._write_equity_snapshot 惰性引用；
                                  shared/indicators.py 是为它保留的 stub）
event_bus ──► 无人引用（死代码, 177 行）
market_data ──► position_manager(_s6api 惰性: get_price/get_symbol_info/get_oi_and_funding/get_rsi),
                position_score（并与其上层 s6_auto_trader 大量同名函数重复）
```

**循环依赖**: 无硬 import 环；但 `PM → trade_recorder → trade_analyzer → binance_api` 与 `shared_executor → PM → …` 形成"策略-执行-记录-分析"横向网，职责上互相穿透（软耦合环）。

---

## 4. S0 Audit

- **定位**: 独立 30s 循环的宏观状态机，进程隔离良好，仅依赖 redis_store + binance_api。
- **输入**: `sample_btc`(4h EMA20/60 趋势、volatility、amp、跌破 EMA60、ATR 扩张) / `sample_breadth`(top50 站上 EMA20 比例) / `sample_alts_sync`(30min) / `sample_shock_score`(60s) / `sample_sentiment`(读 `market:sentiment`)。
- **输出**: Redis `market:s0` + `services/s0/market_state.json` + CH `market_state_log`。
- **Regime 枚举**: `bull_trend(+5) / weak_bull(+3) / range(0) / weak_bear(-3) / risk-off(-7)`，附 `trend_strength`、`s6/s7/s8_allowed`。
- **risk_off 条件**: `(btc_below_ema60 and atr_expanding) or amp>0.04 or breadth_ratio<0.30`。
- **情绪叠加(v1.2)**: 读 `market:sentiment{fng, avg_funding, sentiment_risk, bias}`；`sentiment_risk=True`（FNG≥80/≤15 或 |加权资金费率|>0.05%）→ `regime_score = max(-7, score-1)`。
- **结论**: **已具备独立 Regime Engine 形态**。V2 仅需：抽离自身 fapi_get 副本、把采样函数改为可注入（便于回测）、输出结构加版本化 schema。风险低。
- **未修改**。

## 5. S3 Audit

`strategies/s3_orderflow.py`（1174 行）承担：

| 类别 | 内容 |
|---|---|
| Market Data | Binance REST(24hr/klines)、WS 1m kline(60币,重连)、@trade 大单 WS(仅 btcusdt)、REST fallback 刷新 |
| Feature Engine | EMA/RSI/ATR/量比/taker_buy(k[9])/orderflow_bias，4 窗口(15m/1h/4h/24h)，增量缓存 `_symbol_klines` |
| Event Detector | PULSE/TREND/PANIC/VIOLENT/PUMP/HIGH_VOL/LOW_VOL/ATR_EXPAND/FAILED_BREAKOUT(stateful peak 追踪)，阈值表 THRESHOLDS |
| Event Lifecycle | `_end_expired_events`(EVENT_MAX_AGE)、active/END 状态 |
| Publisher | Redis `event:s3` + `market:s3_data` + pubsub `s3:event:notify` + JSON 兜底 |
| Infrastructure | `get_top_symbols`(成交额 top60)、调度主循环、日志 |

- 职责多但**内聚于"感知"**，无交易行为。V2 拆分方向: collector / features / detector / publisher 四个内部包，单服务不变。
- 隐患: 指标计算与全局可变缓存 `_symbol_klines` 绑定，回测复用难（V2 需要 feature 计算纯函数化）。
- **未修改**。

## 6. S6 Audit

```
Signal(read_all_signals → LONG_SIGNALS 过滤)
  ↓ Gate: long_signal_allows_open(VIOLENT_BULLISH×regime) / event_is_stale / is_event_fresh
          drawdown recovery 限制 / 冷却期 / market_allows_trading(S0) / has_any_position
          趋势过滤(price>1h EMA20) / classify_entry_mode / S6B takeover 条件
          price_is_overextended / MAX_ATR_PCT=6
  ↓ Score: contract_score(strength, flow±5, short_ratio±8/−5, ATR 罚分, extension 罚分, age 罚分)
  ↓ Risk:  leverage_for_score / calc_position_qty(1% 风险帽+ATR 衰减+池化) / bounded_stop_pct(≤8%,takeover≤12%)
  ↓ Position/Execution: shared_executor.open_position → Binance 市价单 + AlgoSL 入队 + pm:positions 注册
```

- SYSTEM_TAG: PULSE_UP/TREND_UP/VIOLENT_BULLISH→S6A，PUMP_UP→S6B（影响 SYSTEM_CFG 选档）。
- **策略层直接调用交易执行**: `S6._open_long → open_position()`（策略=执行，无分离）。所有 gate/score/risk 函数实际定义在 shared_executor，S6 只是编排。
- **未修改**。

## 7. S8 Audit

与 S6 同构，差异点：
- Gate 增加 `short_signal_allows_open`（TREND_DOWN/VIOLENT_BEARISH/PULSE_DOWN 要求 raw_strength≥60，PANIC_SELL 豁免）与 `pump_down_uptrend_guard`（PUMP_DOWN 高周期逆势保护）。
- 无 takeover 分支；stop 基线 0.05（vs S6 0.06）。
- 同样 `S8._open_short → open_position()` 直接执行。
- **未修改**。

## 8. S7 Audit

- 结构: s7_runner(主循环/热更新/持久化) + s7_core(自带 Binance API 层 + MarketDataFeed WS + 余额/挂单/库存) + s7_logic(884 行网格策略) + s0_reader(读 market:s0) + 自带 config/grid.env 与三份设计文档。
- 与引擎核心**零 import 耦合**（仅共享 redis_store 与 market:s0 这个数据契约）。
- **当前未运行**（无 systemd 单元）。
- **保留为 Legacy 的理由**: ①零耦合，重构引擎不会破坏它；②自带 API/配置/热更新机制，迁移成本>收益；③网格策略与动量引擎生命周期不同；④无测试覆盖，动它必然回归风险；⑤其"热更新"设计与 V2 的版本化/可回滚目标冲突，若 V2 期重启 S7 应改为普通部署。
- 动作: **KEEP/DEFER**，整个 V2 期间不动。

## 9. shared_executor Audit

1283 行，职责清单（按 A-J 分类）：

| 类别 | 函数/成员 |
|---|---|
| A. Signal | `read_s3_events` `read_tv_signals` `read_all_signals` `read_s3_market_data` `subscribe_s3_notify` `wait_scan` `event_is_stale` `event_age_sec` `_event_expected_move` |
| B. Market Data | `_get_funding_rate` `get_short_ratio` `_refresh_positions`（隐式行情依赖） |
| C. Decision | `classify_entry_mode` `price_is_overextended` `long_trend_takeover_ready` `pump_down_uptrend_guard` `contract_score` `leverage_for_score` `short_signal_allows_open` `long_signal_allows_open` `resolve_event_flow` `resolve_event_orderflow_bias` `_analysis_allows_open` `_analysis_gate` `_record_analysis_decision` `get_analysis_reject_summary` `_format_analysis_reject_summary` `maybe_log_analysis_panel` |
| D. Risk | `_DD_*` 常量 `_drawdown_status` `drawdown_mode` `maybe_replace_recovery_position` `score_to_fraction` `calc_position_qty` `bounded_stop_pct` `check_global_budget`(否，见下) `_POOL_BUDGET/_RISK_PER_TRADE` 等 |
| E. Position | `load_state/save_state/reconcile_positions` `_update_pos_cache` `_get_positions` `get_position_count` `has_position` `has_any_position` `pm_monitor` `_should_notify_close` `_was_closed_recently` |
| F. Execution | `_fapi_sig` `fapi_get` `fapi_post`（自带签名与沙盘拦截） `open_position`（主执行体, 1052-1247 约 200 行） `_round_qty` `_get_min_notional` |
| G. Notification | `tg_send` `tg_pin` `_log` |
| H. Persistence | `load_state/save_state`(Redis+JSON 双写) `_POS_CACHE` 全局 |
| I. Sandbox | `_sandbox_check/_sandbox_post/_sandbox_get` `_SANDBOX_ACTIVE` |
| J. Other/Infra | 配置解析 `_CONFIG_ENV`、市场状态读取 `get_market_state/market_allows_trading`、全局 `_POS_CACHE` |

- **God Module 定性**: 10 类职责、~60 个顶层符号、单文件被 S6/S8/测试三方消费。
- **V2 去向**: C/D 基本可平移到 `decision/`、`risk/`（多数已是纯函数或近纯函数）；A 迁 `signals/`；F 迁 `execution/`；E 保留为 facade；I 迁 `infra/sandbox`。**必须暂留**: `pm_monitor`（进程内 PM 入口）、`load/save_state`（S6/S8 依赖）、`open_position`（先 ADAPTER 再搬）。
- **未修改**。

## 10. Position Manager Audit

1940 行 / 59 函数。职责盘点：

| # | 职责 | 函数 | 归类 |
|---|---|---|---|
| 1 | Position tracking | `_load/_save/_load_meta/_merge_meta/_merge_meta_preserving_missing` | Position Lifecycle + Infra |
| 2 | Exchange sync | 3 层加载、WS 用户流(选主 `_ws_*`)、`reconcile_all` | Execution/Infra |
| 3 | Hard SL | `_monitor_one` step1 | Risk Policy |
| 4 | Emergency SL | step2 (`sl_breach_max=-5%`) | Risk Policy |
| 5 | Early loss protection | step4 (5min/-2%+15m动量) | Risk Policy |
| 6 | Break even | step6 (`be_done`) | Risk Policy |
| 7 | Partial TP | step7 + `_partial_close` | Risk Policy + Execution |
| 8 | ATR trailing | `_calc_trail_sl/_calc_trail_base/_place_trail_sl/_calc_atr` | Risk Policy |
| 9 | Peak pullback guard | `_peak_pullback_check`（实时锁利推交易所） | Risk Policy |
| 10 | EMA safety | step10 (1h EMA9/20 反转) | Risk Policy |
| 11 | Time stop | step11（含 RSI/资金费延期） | Risk Policy |
| 12 | Algo orders | `_algo_*` worker/enqueue/place/cancel（11s 队列+60s/0.2%节流） | Execution |
| 13 | Ghost cleanup | `_ghost_cleanup/_try_record_ghost_trade` | Execution/Infra |
| 14 | External alert | `_notify_external_position`（含 30s 竞态宽限） | Infra |
| 15 | Symbol filter | `_is_tradable_symbol`（*USDT only） | Infra |
| 16 | Config | `SYSTEM_CFG`(S8A/S8B/S6A/S6B/S6) `_get_cfg` | Risk Policy（参数中心） |
| 17 | Funding guard | step0（±0.5% 强平） | Risk Policy |
| 18 | Redis | `pm:positions` `closed:{sym}` `pm:monitor:writer` 锁 | Infra |
| 19 | Telegram | TG_TOKEN 直发 | Notification |
| 20 | Logging | `_pmlog` 按日文件 | Infra |
| 21 | Persistence on close | `_close → record_trade`（PG+CH+分析入队） | Infra |
| 22 | Stagnant release | `_is_stagnant_profit`（经 exit_factors） | Risk Policy |

- **V2 拆分建议**（本阶段不执行）:
  - `position/infra.py`: 3 层同步、WS 选主、algo 队列、Redis、日志、ghost、record 桥
  - `position/lifecycle.py`: monitor_all/_monitor_one 编排骨架
  - `risk/policy.py`: 11 步退出判定全部抽成纯函数（exit_factors.py 已是这个方向，只有 3 个函数；把 hard/emergency/BE/partial/trail/peak/1h/time 的**判定**与**执行**分离）
  - `risk/policy_config.py`: SYSTEM_CFG 独立成版本化配置
- 最高风险点: `_load/_merge`（交易所↔本地状态合并）与 `_close`（真实下单+记账），Phase 7 之前不动。
- **未修改**。

## 11. TradingView Bridge Audit

`services/tv_bridge.py`（262 行）逐项确认：

| 项 | 现状 |
|---|---|
| webhook 输入 | stdlib ThreadingHTTPServer，POST /webhook + GET /healthz，64KB 上限 |
| secret | fail-closed（未配置拒绝全部），来自 binance.env `TV_WEBHOOK_SECRET` |
| symbol | `normalize_symbol`（剥 `BINANCE:` 前缀与 `.P` 后缀），仅放行 `*USDT` 且非 `_PERP` |
| signal | 9 个映射到 S3 事件词表（TV_SIGNAL_MAP），未知即拒 |
| strength | clamp 0-99，默认 50 |
| bar_close | 依赖 TV 侧 `freq_once_per_bar_close`；桥内无 bar 时间戳字段（仅有 ts+去重） |
| flow | `taker_buy_ratio`(0-1)/`orderflow_bias`(-1..1) 越界丢弃后透传 |
| dedup | `tv:dedup` 5 分钟/ (signal,symbol)，顺带清理过期项 |
| 币池对齐 | 只转发 `market:s3_data` 现役 60 币内的信号；池空时宽松放行；`TV_REQUIRE_IN_POOL=0` 可关 |
| Redis | `event:tv` 快照（90s 窗口、≤20 条）+ `s3:event:notify` 唤醒 |
| S6/S8 消费 | `read_tv_signals()`（注入 `_snapshot_ts=event.ts`，走同一过期判定）→ 同一 Gate 链 |

**V2 Signal Adapter 判定: 可以直接作为 V2 的 Signal Adapter 原型**（职责单一、fail-safe、与引擎解耦）。

遗留问题（V2 改进项，均不阻塞）:
1. HTTP 处理与业务逻辑同文件（可拆 handler/adapter）。
2. 幂等只靠时间窗去重，TV payload 的 bar 时间戳未参与幂等键（重复 alert 换强度值可绕过 5min 窗）。
3. 事件词表与 S3 强耦合（V2 应显式定义 Signal Schema 版本）。
4. `event:tv` 快照结构复用 event:s3 的约定是隐式契约，无 schema 校验。

**未修改**。

## 12. Replay Audit

**结论：当前不存在决策级 Replay 框架。**

现存的三样东西都不是 Replay：
1. `trade_analyzer` T0/T15/T60 —— **事后复盘**（对已成交交易算 MFE/效率），不是决策重放。
2. `scripts/sandbox.py` —— **实时纸面交易**（拦截实时 API），不能喂历史数据。
3. `exit_factors.py` —— 3 个纯函数（1h反转/早期亏损动量/停滞止盈），docstring 声明供回测用，PM 在用且有单测。这是唯一"可回放"的种子。

**V2 关键缺口**: 重构 S6/S8/PM 后，**目前无法证明"新代码与旧代码交易决策一致"**。Gate 链的输入（event + market 快照 + state + 配置）没有被留档，无法离线重放。

**V2 Phase 1 必须先建**:
- Decision Journal: S6/S8 每次评估（含每个 gate 的输入与判定结果、score、qty、拒绝原因）落 CH/JSONL
- Replay Runner: 用 Journal 或历史 `event:s3`/`market:s3_data` 快照离线重放同一批输入，对比新旧 gate 输出逐字段 diff
- PM 侧: 用 `trade_analysis` 已有数据 + `exit_factors` 纯函数先行覆盖退出判定回归

## 13. Redis / Database Dependency

**Redis key 清单（实际 grep 汇总，无中央注册表）**:

| Key | 写/读方 | 用途 |
|---|---|---|
| `event:s3` `market:s3_data` `s3:event:notify` | S3 写 / S6S8 读 | 事件与行情快照 |
| `event:tv` `tv:dedup` | tv_bridge / S6S8 | TV 信号 |
| `market:s0` | S0 写 / S6S8 读 | regime |
| `market:sentiment` | sentiment_bridge 写 / S0 读 | 情绪 |
| `pm:positions` | PM(经 shared_executor 注册) | 持仓元数据 |
| `closed:{symbol}` | PM 标记/读 | 防重平仓 4h |
| `pm:monitor:writer` | S6S8 锁 | PM 单写者 |
| `pm:ghost_close:{sym}` | PM 锁 | 幽灵清理互斥 |
| `ws:userstream:leader` 等 | PM WS 选主 | 单连接 |
| `state:s6` `state:s8` | shared_executor | 执行器状态(cooldowns) |
| `account:peak` `account:dd_pause` | shared_executor | 回撤熔断 |
| `cd:loss` | trade_recorder | 亏损冷却 |
| `checkpoint:pnl` | trade_recorder | 周期盈亏起点 |
| `event:analysis_reject` | shared_executor | 分析过滤命中 |
| `cache:s3_rolling` `pool:candidate` `signal:s2_latest` `breaker:circuit` `state:trader` `share:positions` `mover:s3_spot` `log:trade` | **仅 s6_auto_trader（legacy）** | 遗留 |

- JSON 文件双写: S3（`strategies/config/s3_*.json`）、S6/S8 state（`S6_state.json`/`S8_state.json`）、S0（`market_state.json`）——Redis 与文件双持久化，无单一事实源。
- **PostgreSQL**: `trade_episodes`/`trade_events`（env 列已加）；schema 在 `db/postgres_schema.sql`；`trade_episode_attribution` 视图依赖 `decision_context`。
- **ClickHouse**: `trade_history`(31列,含env) / `trade_analysis`(T0/T15/T60,含env) / `market_state_log` / `equity_snapshot`(经 s6_auto_trader 旧路径)。
- 双写顺序: record_trade 先 PG upsert 再 CH insert，无事务保障（可接受，台账以 PG 为准）。

## 14. Execution Dependency

**存在 4 套独立的 Binance API 实现**（V2 最大重复面）：

| 实现 | 位置 | 使用方 |
|---|---|---|
| A | `shared/binance_api.py`（sign/fapi_get/post/delete + sandbox 拦截 + health.record） | S3、S0、tv_bridge、trade_recorder、postgres_client(get_env) |
| B | `strategies/shared_executor.py` 内部 `_fapi_sig/fapi_get/fapi_post` + 自带 sandbox 拦截 | S6/S8 全部交易调用 |
| C | `shared/position_manager.py` `_light_fapi_get/post/delete` + `_light_get_price` | PM 全部 |
| D | `services/s7/s7_core.py` 自带 sign/fapi_* | S7（legacy） |

- **Sandbox 拦截点有 3 处**（binance_api / shared_executor / s6_auto_trader），行为可能不一致。
- 下单路径: S6/S8 → shared_executor.open_position(B) → 市价单；止损经 PM algo 队列(C, 11s 节流) + PM 轮询兜底。轮询与 algo 单双保险但实现分散。
- Binance demo/prod 切换: 仅 `BINANCE_TESTNET` → binance_api.FAPI（B/C 层各自引用 binance_api.FAPI，但自己签名）。

## 15. Current Test Baseline

```
pytest -q
106 passed, 0 failed, 0 skipped, 0 error   (0.9s)
```

- 测试文件(10): test_position_manager / test_shared_executor / test_trade_recorder / test_trade_analyzer / test_tv_bridge(15) / test_sentiment_bridge(6) / test_sandbox / test_s3_orderflow / test_s3_live_window / test_postgres_client + conftest(FakeRedis/patch_pm/patch_executor)
- 覆盖面: PM 退出链单测、shared_executor 风控单测、记账/分析、两个 bridge、S3 事件、PG 客户端、沙盘。
- **无覆盖**: S7（0 测试）、S0（0 测试）、S3→S6/S8 集成、PM `_load/_merge` 交易所合并路径（最高危区域无直接测试）。
- 备注: `test_pm_monitor_does_not_send_cache_based_close_message` 历史上出现过偶发失败（本轮通过），建议 V2 排查其时序依赖。

## 16. Current Trading Behavior Baseline

- **运行状态**: `PAUSE_OPEN` 激活（本次升级前人为置入），所有 `open_position` 第一步返回 False；持仓为零（PM 与交易所双侧确认）；交易所无挂单。
- **环境**: `env=demo`（BINANCE_TESTNET=true, demo-fapi）；余额 4039.70 USDT。
- **v1 最终行为基线（近 7 天，历史数据）**: 62 笔、胜率 50%、净 +44.9U；盈利单退出效率 72~79%（峰值锁利生效）；早期亏损保护 22 笔 -272U（MFE 仅 0.62%，进场即逆）；S8 空 +96U / S6A 多 -51U。
- **关键行为参数（V2 不得漂移）**: 短空强度≥60；紧急止损-5%；早期保护 5min/-2%；be_done +2%(S8A)；partial TP {5:0.3}；峰值锁利 trigger3%/pullback2%(S8A)；回撤熔断 8%/15%/恢复0.25；单笔风险 1%；池化 80%/单仓15%；冷却 2h/4h(亏损)/4h 重开。

## 17. God Modules

1. **shared_executor.py**（1283 行, 10 类职责）— Signal+Data+Decision+Risk+Position+Execution+Notify+Persistence+Sandbox+Config。
2. **position_manager.py**（1940 行, 59 函数, 22 类职责）— Lifecycle+全部退出策略+交易所同步+Algo基建+记账+通知。
3. **s6_auto_trader.py**（2375 行）— legacy 巨石，与 market_data.py 大量同名函数重复；仅剩 1 个活引用。
4. **s3_orderflow.py**（1174 行）— 6 类职责但内聚于感知层，风险中等。

## 18. Tight Coupling

1. **策略=执行**: S6/S8 直接调 `open_position`（策略层直接下市价单）。
2. **PM 寄生于策略进程**: `pm_monitor()` 在 S6/S8 主循环内调用，PM 无独立进程；靠 `pm:monitor:writer` 锁 + system_filter 防双跑。
3. **PM→trade_recorder→PG/CH**: 退出策略与记账/通知同函数路径（`_close` 尾部 record_trade）。
4. **shared_executor ↔ PM 双向**: shared_executor 导入 PM（monitor_all/_load），PM 的 `_s6api` 又兜底引用 trade_recorder/s6_auto_trader 链。
5. **TV 事件词表 = S3 词表**（隐式契约）。

## 19. Hidden Dependencies

1. **sys.path 双身份**: S6/S8 用 `sys.path.insert` 后 `from shared_executor import`（顶层名）；测试用 `from strategies.shared_executor import`。同一文件可能以两个模块对象并存，模块级全局（`_POS_CACHE`、`_SANDBOX_ACTIVE`、`_analysis_filter_cache`）可能分裂。
2. **4 套 Binance API + 3 个 sandbox 拦截点**（§14）——改其中一套不会同步其他。
3. **config/binance.env 被 4 处各自解析**（binance_api.load_config / shared_executor._CONFIG_ENV / clickhouse_client._cfg / postgres_client._config_value），无统一 config 层。
4. **exit_factors.py** 是唯一"纯函数化"的退出判定，PM 其余 8 步退出判定仍内联在 `_monitor_one`。
5. **s6_auto_trader 假活**: trade_recorder._write_equity_snapshot 惰性 import 它，导致 2375 行 legacy + shared/indicators stub 无法删除。
6. **event_bus.py / market_data.py 部分函数 / position_score(部分) 为半死代码**，但 market_data 与 position_score 仍有活引用，不能直接删。
7. **JSON 兜底持久化**与 Redis 并存（S3/S6/S8/S0），恢复顺序语义未定义。
8. **S0 写 market_state.json 文件**，但主要消费方读 Redis——文件是给谁的历史遗留未明。

## 20. V2 Migration Risk

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | 无决策 Replay，重构后无法证明行为等价 | **最高** | Phase 1 先建 Decision Journal + Replay Runner，先有验证再动刀 |
| R2 | PM `_load/_merge/_close` 是真钱路径（状态合并、防重平仓、部分成交累积），无直接单测 | 高 | 先补该路径单测与金样本，再谈拆分；Phase 7 前不动 |
| R3 | sys.path 双模块身份 → 全局状态分裂（`_POS_CACHE` 等） | 高 | V2 统一为包导入（strategies.*），提供兼容 shim |
| R4 | 4 套 API/3 个沙盘拦截合并时行为差异（重试、签名、health 打点、沙盘判定顺序） | 高 | 以 shared/binance_api 为唯一实现，B/C 变薄委托，逐层回放验证 |
| R5 | 参数散落 8+ 处（SYSTEM_CFG/S6/S8 常量/THRESHOLDS/env 解析×4），迁移中参数漂移=隐性改策略 | 中高 | 建 config registry + 快照对比测试（参数冻结清单见 §16） |
| R6 | Redis/JSON 双持久化，迁移期新旧双写不一致 | 中 | 单一事实源 + 版本化 key |
| R7 | demo 数据特性（taker=0、funding=0、怪异余额）干扰新过滤逻辑验证 | 中 | env 标签已就绪；验证用例须区分 demo/prod 数据集 |
| R8 | 自动 daily push cron（21:00 git add -A）会把半成品提交推送 | 中 | V2 期间迁移到 feature 分支或暂停 cron |

## 21. Recommended Migration Order

```
Phase 1  回归基建: Decision Journal + Replay Runner + PM _load/_close 金样本单测   （不碰任何生产行为）
Phase 2  Signal Adapters: signals/ 包成型，tv_bridge/sentiment_bridge/S3 publisher 对齐统一 Signal Schema
Phase 3  Decision/Risk 抽取: shared_executor 的 C/D 类函数 → decision/ risk/（原位置留 re-export shim）
Phase 4  Execution 抽取: open_position → execution/（shared_executor 变 facade）
Phase 5  S3 内部拆分: collector / features / detector / publisher（单服务不变）
Phase 6  S0 → regime/ 独立引擎包（纯函数化采样，可回测）
Phase 7  PM 拆分: infra(同步/algo/redis) / lifecycle / policy（先 replay 证明退出等价再动）
Phase 8  Legacy 清理: s6_auto_trader（先重写 equity snapshot 依赖）、event_bus、重复 indicators、market_data 去重
S7      全程 DEFER/KEEP
```

原则: 渐进迁移 + 新旧并存（shim/facade）+ 每阶段可回滚 + 每阶段 Replay diff 全绿才进下一阶段。

## 22. Files That Must NOT Be Modified In Phase 1

Phase 1 只允许新增（replay/journal/测试），以下为禁改清单：

```
shared/position_manager.py        # 真钱路径，R2
shared/position_score.py
shared/exit_factors.py            # 已是纯函数，Phase 1 只补测试不改动
shared/trade_recorder.py
shared/trade_analyzer.py
shared/binance_api.py             # 4 套 API 合并前不动
shared/redis_store.py
shared/postgres_client.py
shared/clickhouse_client.py
shared/market_data.py
shared/data_cache.py
strategies/shared_executor.py
strategies/S6.py
strategies/S8.py
strategies/s3_orderflow.py
services/s0/s0_market_guard.py
services/s0/indicators.py
services/s7/**                    # 全程 DEFER
services/tv_bridge.py             # Phase 2 才动
services/sentiment_bridge.py      # Phase 2 才动
scripts/sandbox.py
db/postgres_schema.sql            # 表结构冻结（env 迁移已完成）
config/binance.env                # 含密钥与环境开关
monitoring/metrics_collector.py
indicators.py / shared/indicators.py / __init__.py   # legacy stub，清理在 Phase 8
```

---

## 附: V2 迁移矩阵

| 当前模块 | 当前职责 | V2目标模块 | 动作 | 风险 | Phase |
|---|---|---|---|---|---|
| strategies/shared_executor.py | Signal+Decision+Risk+Execution+State+Notify+Sandbox（God） | signals/ decision/ risk/ execution/ + facade | SPLIT | HIGH | Phase 3-4 |
| shared/position_manager.py | Lifecycle+退出策略+交易所同步+Algo+记账桥（God） | position/infra + position/lifecycle + risk/policy | SPLIT | HIGH | Phase 7 |
| strategies/s3_orderflow.py | 行情+特征+事件+发布+调度 | market_data/ features/ detector/ publisher/（单服务） | SPLIT | MEDIUM | Phase 5 |
| services/s0/s0_market_guard.py | Regime 引擎（已近独立） | regime/ 包（采样纯函数化） | REFACTOR | LOW | Phase 6 |
| services/tv_bridge.py | 外部信号接入 | signals/adapters/tv | ADAPTER | LOW | Phase 2 |
| services/sentiment_bridge.py | 情绪采集 | signals/adapters/sentiment（或 infra/feeds） | ADAPTER | LOW | Phase 2 |
| shared/exit_factors.py | 3 个纯退出因子 | risk/policy.py（扩容为全 11 步纯判定） | EXTRACT | LOW | Phase 3/7 |
| strategies/S6.py / S8.py | 策略编排（含直接执行） | strategies 只留编排，执行走 execution/ | REFACTOR | MEDIUM | Phase 4 |
| shared/binance_api.py | API 实现 A | infra/exchange/ 唯一实现 | REFACTOR | HIGH | Phase 4（合并 B/C 之后） |
| shared_executor 内部 fapi（B）/ PM _light_fapi（C） | 重复 API 层 | 并入 infra/exchange/ | EXTRACT | HIGH | Phase 4 |
| services/s7/** | 网格策略（未运行，自包含） | 保持原样 | KEEP/DEFER | LOW | Phase 8 后评估 |
| shared/s6_auto_trader.py | legacy 巨石（1 个惰性引用） | 无（重写 equity snapshot 后删除） | DEFER→删除 | MEDIUM | Phase 8 |
| shared/event_bus.py | 死代码（0 引用） | 无 | DEFER→删除 | LOW | Phase 8 |
| shared/market_data.py / position_score.py | 半活跃，与 legacy 重复 | infra/market 或并入 features | SPLIT | MEDIUM | Phase 5/8 |
| indicators.py ×3 | 重复/存根 | infra/indicators 单份 | REFACTOR | LOW | Phase 8 |
| db/postgres_schema.sql + CH 表 | 台账+分析（env 已就绪） | 不变（Phase 1 或加 journal 表） | KEEP | — | — |
| scripts/sandbox.py | 纸面交易 | infra/sandbox（3 拦截点归一） | REFACTOR | MEDIUM | Phase 4 |
| monitoring/metrics_collector.py | Prometheus 指标 | 不变 | KEEP | LOW | — |
| config 解析（×4 处） | 散落配置 | infra/config registry | EXTRACT | MEDIUM | Phase 3 |
| tests/（106 通过） | 单测基线 | + replay 回归集 | KEEP+扩充 | — | Phase 1 |
| 运行时状态（PAUSE_OPEN、pm:positions、*_state.json） | 运维开关/状态 | 不变 | KEEP | — | — |
