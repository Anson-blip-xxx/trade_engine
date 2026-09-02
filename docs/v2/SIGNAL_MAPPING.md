# Signal Mapping（P2 · Legacy → Unified）

> 每一项均来自真实代码（见 SIGNAL_INVENTORY.md）。不确定的字段标 NEEDS DECISION。

## 1. 事件 dict → Unified Signal

| Legacy 字段（S3 / TV event dict） | Unified Signal 字段 | 归一规则 | 备注 |
|---|---|---|---|
| `evt['symbol']` | `Signal.symbol` | strip + upper | 缺失/空 → SignalValidationError |
| 消费方硬编码（S6→LONG / S8→SHORT） | `Signal.side` | 大写；BUY→LONG、SELL→SHORT | TV `evt['side']` 保留在 metadata（消费方不读） |
| `evt['type']` | `Signal.signal_type` | strip + upper | S3/TV 词表一致 |
| `signal_source_from_event(evt)`：无标记→S3，'tv'→TRADINGVIEW | `Signal.source` | strip + upper；开放集合（不 enum 限死） | 与 journal/builder 同规则 |
| 消费方 NAME（'S6'/'S8'） | `Signal.strategy` | strip + upper | 与 source 分离 |
| `evt.get('strength')` | `Signal.strength` | float 化；解析失败 → None | raw 值，不做阈值判断 |
| `evt.get('ts')`（仅 TV） | `Signal.timestamp` | epoch 秒 → UTC ISO-8601 | S3 事件无 ts → None |
| `evt.get('event_id')` | `Signal.event_id` | 原样 | 当前恒 None |
| `evt`（整包，含 chg_*/vol_ratio/taker_buy_ratio/tv_signal/price/comment/_snapshot_ts） | `Signal.metadata` | 浅拷贝 dict | 原始事件字段快照，非垃圾场 |

## 2. Unified Signal → Journal（映射边界，Journal 本身不改）

| Signal 字段 | DecisionJournalBuilder kwargs → SignalSnapshot |
|---|---|
| `signal.source` | `signal_source` → `SignalSnapshot.source` |
| `signal.signal_type` | `signal_type` → `SignalSnapshot.signal_type` |
| `signal.symbol` | `symbol` → `SignalSnapshot.symbol` |
| `signal.side` | `side` → `SignalSnapshot.side` |
| `signal.strength` | `strength` → `SignalSnapshot.strength` |
| `signal.timestamp` | `signal_timestamp` → `SignalSnapshot.signal_timestamp` |
| `signal.event_id` | `event_id` → `SignalSnapshot.event_id` |
| `signal.metadata` | `raw` → `SignalSnapshot.raw`（完整原始事件） |
| — | `SignalSnapshot` 无 strategy 字段（NEEDS DECISION，Phase 3+） |

行为保持说明：P1-02 中 S6/S8 直接把 evt 字段喂给 Builder；P2 起改为经 Signal 映射，
两者产出的 SignalSnapshot 字段值**逐字段相同**（由 integration 测试锁定）。
唯一有意差异：TV 事件（带 `ts`）从此记录 `signal_timestamp`（此前恒 None）——
TV 通路当前休眠，属记录完善而非行为变更。

## 3. 明确不映射（NEEDS DECISION / 不属于 Signal）

| Legacy | 去向 | 说明 |
|---|---|---|
| `contract_score` 输出 `_score` | Decision/Strategy 层 | 含 ATR/extension/age 调整，非 Signal 自带 |
| `entry_mode`（RIGHT_MOMENTUM 等） | Decision 层 | 入场分类是决策结果 |
| PM 持仓字段 `event_type` | PM legacy 字段 | = 开仓时 signal_type（OBS-2）；PM 不动 |
| `order_side` BUY/SELL | Execution 层 | 订单动作，非信号方向 |
| `_snapshot_ts` | Signal.metadata | 快照注入时间，非信号产生时间 |
| `tv_signal`（TV 原始信号名） | Signal.metadata | 与 signal_type 同值（TV_SIGNAL_MAP 的 key） |

## 4. Adapter 一览

| Adapter | 输入 | 输出 | 使用方 |
|---|---|---|---|
| `signal_from_event(evt, side, strategy)` | S3/TV 事件 dict | Signal | 通用 |
| `s6_signal(evt)` | S6 消费的事件 | Signal(side=LONG, strategy=S6) | S6._open_long |
| `s8_signal(evt)` | S8 消费的事件 | Signal(side=SHORT, strategy=S8) | S8._open_short |
| `to_journal_builder_kwargs(signal)` | Signal | DecisionJournalBuilder kwargs | S6/S8 Journal 旁路 |
