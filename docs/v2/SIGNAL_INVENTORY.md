# Signal Inventory（P2 · 基于实际代码统计，非猜测）

> 统计时间：Phase 2 开始时。只记录**实际存在**的 Signal 来源与表达。

## 1. 来源清单

| # | Source | 产出位置 | 输入形态 | 消费方 | 当前 side 表达 | 当前 type 表达 | strength | timestamp |
|---|--------|---------|---------|--------|---------------|---------------|----------|-----------|
| 1 | **S3** | `strategies/s3_orderflow.py detect_events()` → Redis `event:s3` → `read_all_signals()` | **plain dict**：`{type, symbol, strength, chg_15m/1h/4h/24h, vol_ratio, ...}`（无 ts、无 side） | S6 / S8 | **无**——由消费方决定（S6→LONG / S8→SHORT） | `evt['type']`：PULSE_UP/DOWN, TREND_UP/DOWN, PANIC_SELL, VIOLENT_BULLISH/BEARISH, PUMP_UP/DOWN, FAILED_BREAKOUT, HIGH_VOL/LOW_VOL, ATR_EXPAND | `evt['strength']` int 15~99 | **无**（快照注入 `_snapshot_ts`） |
| 2 | **TradingView** | `services/tv_bridge.py` → Redis `event:tv` → `read_all_signals()` | **plain dict**：`{type, symbol, strength, ts, source:'tv', tv_signal, side, price?, taker_buy_ratio?, orderflow_bias?, comment?}` | S6 / S8 | `evt['side']`（LONG/SHORT）——**但 S6/S8 不读它**，消费方硬编码 side | `evt['type']`（与 S3 词表一致） | `evt['strength']` 0~99 | `evt['ts']` epoch 秒 |
| 3 | **S6 消费上下文** | `strategies/S6.py _open_long` | —（side/strategy 是消费方常量） | — | 硬编码 `'LONG'`；system_tag：PULSE_UP/TREND_UP/VIOLENT_BULLISH→S6A，PUMP_UP→S6B | = `evt['type']` | `evt.get('strength')`（raw）→ `contract_score(...)` 产出 `_score` | — |
| 4 | **S8 消费上下文** | `strategies/S8.py _open_short` | — | — | 硬编码 `'SHORT'` | = `evt['type']` | 同上 | — |
| 5 | **Journal SignalSnapshot**（记录表达） | `journal/models.py` | frozen dataclass | PM 台账 / Replay | `side`：LONG/SHORT/NEUTRAL/UNKNOWN | `signal_type` | `strength` float\|null | ISO-8601 |
| 6 | **shared_executor decision_context**（决策遥测） | `open_position(decision_context=...)` | dict | PG `trade_events` | 无 | `signal_type`（= 开仓信号类型） | `raw_strength`（raw）+ `strength`（contract_score 输出） | 无 |

## 2. 现存 side 值统计（真实代码）

| 值 | 出现位置 | 语义 |
|---|---|---|
| `LONG` / `SHORT` | S6/S8 硬编码、PM 持仓 `side` 字段、TV 事件 `side`、journal `Side` enum | **交易方向**（Signal 语义） |
| `BUY` / `SELL` | `open_position order_side`、`_close close_side`、`_partial_close close_side`、AlgoSL side | **订单动作**（Execution 语义，非 Signal） |
| `NEUTRAL` / `UNKNOWN` | journal `Side` enum | 预留 |

结论：Signal side 统一为 `LONG / SHORT / NEUTRAL / UNKNOWN`；BUY/SELL 属 Execution，
由 Adapter 转换（BUY→LONG、SELL→SHORT），原代码不动。

## 3. signal_type / event_type 语义（来自 PM_GOLDEN_OBSERVATIONS OBS-2）

| 字段 | 写入方 | 读取方 | 语义 |
|---|---|---|---|
| `signal_type` | `PM.open_position`、Journal SignalSnapshot、decision_context | Journal | **开仓信号类型**（PULSE_UP 等） |
| `event_type` | `shared_executor._update_pos_cache`（S6/S8 生产路径） | `_close` 记账 | **同一语义**，历史字段名 |

结论：两者语义相同、名称不同（历史遗留）。Unified Signal 采用 **`signal_type`**；
`event_type` 是 PM 持仓记录的 legacy 字段名，映射关系记录于 SIGNAL_MAPPING.md，
PM 代码不动（Golden Tests 已冻结）。

## 4. score / strength 归属

| 值 | 产生位置 | 归属 |
|---|---|---|
| `evt['strength']` | S3/TV 原始信号 | **Signal**（信号自带的原始强度） |
| `_score = contract_score(...)` | S6/S8（含 ATR/extension/age/flow 调整） | **Strategy/Decision 层**（不进 Signal） |
| `raw_strength` | decision_context（= evt strength） | 决策遥测 |

## 5. 不属于 Signal 的数据（明确排除）

- Market：ATR/EMA/RSI/taker_buy_ratio/vol_ratio/chg_*（→ Journal MarketSnapshot / 决策上下文）
- Regime：S0 regime（→ RegimeSnapshot）
- Risk：仓位/杠杆/止损（→ RiskSnapshot）
- Execution：BUY/SELL、订单参数（→ Execution）
- Position：qty/entry/PnL（→ PM）

以上字段当前随原始事件 dict 流动（如 `chg_1h`），Unified Signal 将其保留在
`metadata`（原始事件快照），**不升级为核心字段**。

## 6. Adapter 需求结论

| 来源 | Adapter | 说明 |
|---|---|---|
| S3 event dict | ✅ `signal_from_event(evt, side, strategy)` | 消费方侧适配（不改 S3/Redis payload） |
| TradingView event dict | ✅ 同上（source 归一 TRADINGVIEW，ts→ISO） | tv_bridge 已做一层归一，不重复建设 |
| S6 / S8 消费上下文 | ✅ `s6_signal(evt)` / `s8_signal(evt)` | 薄封装（side/strategy 常量注入） |
| Manual / Replay / AI | ❌ 不虚构 | 当前不存在实际产出点 |
