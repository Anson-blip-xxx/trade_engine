# Decision Journal Schema（v1.0）

> V2 Migration · P1-01 产物
> 代码位置：`journal/`（models / serializer / schema）
> 测试位置：`tests/journal/`
> 状态：**仅数据模型**，未接入任何交易链路，未接任何存储

---

## 1. 为什么需要 Decision Journal

V1 系统的信号决策过程是"黑盒"：S6/S8 收到事件后跑完 Gate 链直接开仓或跳过，
只留下人类可读的日志行（`[S8] MUBARAKUSDT TREND_DOWN strength=36 < 60，短空确认不足，跳过`）。

这带来三个已经付出过代价的问题：

1. **无法回归**：V2 重构 Gate 链 / Risk / PM 后，没有任何机制能证明"新代码与旧代码对同一输入做出相同决策"。
2. **无法归因**：事后只有成交结果，没有"当时每个 Gate 看到什么值、卡在哪一步"的结构化数据。
3. **无法统计**：想做胜率/质量/信号来源的滚动分析，只能靠日志正则或零散的 CH 表。

Decision Journal 是这三种能力的共同底座：**用统一的、不可变的、可序列化的格式，
记录一次信号经过决策链的完整过程**。

## 2. Journal 生命周期

```
Signal 到达（S3 / TradingView / AI / Manual / Replay）
        │
        ▼
策略决策链开始（P1-02 起由 Adapter 产出）
        │
        ├─ 记录 SignalSnapshot   （信号输入）
        ├─ 记录 MarketSnapshot   （当时行情快照）
        ├─ 记录 RegimeSnapshot   （当时 S0 状态）
        ├─ 逐个 Gate 判定        → 每步记录 GateResult（保持执行顺序）
        ├─ Risk 层通过时         → 记录 RiskSnapshot（否则为 null）
        └─ 决策结论              → DecisionResult（OPEN / REJECT / ...）
        │
        ▼
DecisionJournal 组装完成 → 不可变（frozen）
        │
        ▼
to_dict() / to_json()   → 交给 Writer（P1-02 决定：文件 / CH / 内存）
        │
        ▼
from_dict() / from_json() → Replay 或分析侧读回
```

边界（本阶段）：
- **不写**：Redis / PostgreSQL / ClickHouse / 文件 / 任何 MQ
- **不接**：S3 / S0 / S6 / S8 / PM / shared_executor / TV Bridge 均未 import journal
- **不算**：模型层不取行情、不重算指标、不重算 Risk，只记录

## 3. 字段说明

### 3.1 DecisionJournal（根对象）

| 字段 | 类型 | 必须 | 说明 |
|---|---|---|---|
| journal_version | string | ✅ | Schema 版本，如 `"1.0"`。**不用 "v2" 这类名称**，数字递增 |
| journal_id | string | ✅ | 全局唯一（UUID4），不依赖数据库自增 |
| created_at | ISO-8601 UTC | ✅ | Journal 创建时间 |
| decision_at | ISO-8601 UTC | ✅ | 决策实际发生时间（可与 created_at 相同，但字段必须独立保留） |
| signal | SignalSnapshot | ✅ | 信号输入 |
| market | MarketSnapshot | — | 行情快照 |
| regime | RegimeSnapshot | — | 市场状态 |
| strategy | StrategySnapshot | — | 策略上下文 |
| gates | GateResult[] | — | **保持执行顺序**（stage 1..N 连续），Replay 依赖此顺序 |
| decision | DecisionResult | ✅ | 最终结论 |
| risk | RiskSnapshot \| null | — | Risk 层输出；信号在 Risk 之前被拒时为 **null** |
| metadata | Metadata | — | 环境/进程/关联 ID |

### 3.2 SignalSnapshot

| 字段 | 类型 | 说明 |
|---|---|---|
| source | string | 文档化取值：`S3 / TRADINGVIEW / AI / MANUAL / REPLAY / UNKNOWN`。**模型层不做硬限制**（source-agnostic，未来新来源直接传字符串即可） |
| signal_type | string | 如 `PULSE_UP`、`TREND_DOWN`。不在模型层硬编码全集（未来可加新信号） |
| symbol | string | 项目当前格式（大写，如 `BTCUSDT`） |
| side | string | `LONG / SHORT / NEUTRAL / UNKNOWN`。**不用 BUY/SELL**——Journal 描述交易方向，不是订单动作 |
| strength | number \| null | 原始强度值（当前系统 0~100），**不做换算** |
| signal_timestamp | ISO-8601 \| null | 信号产生时间 |
| event_id | string \| null | 事件去重/关联 ID |
| raw | object | 原始 signal payload，必须 JSON 可序列化（不保存 Python 对象） |

### 3.3 MarketSnapshot

全部行情字段允许 null：`price / bid / ask / spread / volume / volume_ratio / atr / atr_pct /
ema20 / ema50 / rsi / taker_buy_ratio / open_interest / funding_rate`。
当前系统没有的字段填 **null**，**不要为了填字段而调 Binance**。
`extra` 用于未来扩展（orderbook 等），必须 JSON 可序列化。

### 3.4 RegimeSnapshot

| 字段 | 说明 |
|---|---|
| regime | 如 `weak_bull`；不做枚举硬限制 |
| risk_level | 建议 `LOW / NORMAL / HIGH / CRITICAL` |
| confidence | 0~1；当前 S0 没有此概念则 **null** |
| source | 如 `S0` |
| extra | 未来扩展 |

### 3.5 StrategySnapshot

`strategy`（如 S6）/ `version`（策略版本）/ `score`（原始值，不假设 0~100）/
`entry_mode`（如 RIGHT_MOMENTUM）/ `timestamp` / `extra`。

### 3.6 GateResult（核心）

| 字段 | 类型 | 说明 |
|---|---|---|
| name | string | Gate 名（小写），如 `fresh / cooldown / position_limit / market_allowed / trend / atr / orderflow / score` |
| stage | int | 执行顺序，**从 1 开始且连续**；校验强制 |
| passed | bool | 是否通过 |
| value | any | Gate 实际取值（JSON 值：bool/数字/字符串/对象均可） |
| threshold | any \| null | 阈值 |
| reason | string \| null | **失败时必须尽量提供**（如 "ATR exceeds maximum"） |
| timestamp | ISO-8601 \| null | 判定时间 |
| duration_ms | number \| null | 本阶段不强制采集，允许 null |
| metadata | object | 扩展 |

### 3.7 DecisionResult

| 字段 | 说明 |
|---|---|
| action | `OPEN / REJECT / HOLD / CLOSE / NO_ACTION`（本阶段主要用于开仓决策） |
| accepted | bool |
| reason | 如 `atr_gate_failed` |
| final_score | number \| null |
| timestamp | ISO-8601 |

### 3.8 RiskSnapshot

`risk_level / position_size / position_fraction / leverage / max_loss / stop_pct /
take_profit_pct / timestamp / metadata`。全部数字允许 null。**模型层不重算 Risk，只记录。**

### 3.9 Metadata

| 字段 | 说明 |
|---|---|
| environment | `SANDBOX / PAPER / LIVE / REPLAY` |
| hostname / process / pid | 进程信息 |
| git_commit | 由**调用方提供**，模型层不强制自动读 Git |
| config_version | 配置版本 |
| correlation_id | Signal → Decision → Order → Position 串联 ID |
| parent_signal_id | 上游信号 ID |
| extra | 扩展 |

## 4. JSON Schema（Draft 2020-12）

权威定义在 `journal/schema.py` 的 `DECISION_JOURNAL_SCHEMA`（含 `$defs` 全部子结构），
与 `docs/v2/` 本文档保持同版本。要点：

- 根对象 `additionalProperties: false`，`required: [journal_version, journal_id, created_at, decision_at, signal, decision]`
- 各快照对象同样 `additionalProperties: false`，**扩展一律通过各自的 `extra` 字段**（不破坏旧 Schema）
- Gate `stage`: `integer, minimum 1`；Gate `required: [name, stage, passed]`
- `side` 枚举：`LONG / SHORT / NEUTRAL / UNKNOWN`；`action` 枚举：`OPEN / REJECT / HOLD / CLOSE / NO_ACTION`
- ISO-8601 时间用 pattern 校验：`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$`
- 测试（`tests/journal/test_schema.py`）强制 Schema 属性与 dataclass 字段一一同步，防止文档/代码漂移

## 5. 完整 JSON 示例

```json
{
  "journal_version": "1.0",
  "journal_id": "0f0e1d2c-3b4a-4958-8677-112233445566",
  "created_at": "2026-09-01T12:00:00.123Z",
  "decision_at": "2026-09-01T12:00:00.123Z",

  "signal": {
    "source": "S3",
    "signal_type": "PULSE_UP",
    "symbol": "BTCUSDT",
    "side": "LONG",
    "strength": 78.0,
    "signal_timestamp": "2026-09-01T12:00:00.123Z",
    "event_id": "evt-001",
    "raw": {"type": "PULSE_UP", "chg_15m": 3.2}
  },

  "market": {
    "symbol": "BTCUSDT",
    "price": 112300.5,
    "bid": 112300.4,
    "ask": 112300.6,
    "spread": 0.2,
    "volume": 12345.6,
    "volume_ratio": 1.8,
    "atr": 1200.0,
    "atr_pct": 0.0107,
    "ema20": 111900.0,
    "ema50": 111500.0,
    "rsi": 64.2,
    "taker_buy_ratio": 0.57,
    "open_interest": null,
    "funding_rate": null,
    "timestamp": "2026-09-01T12:00:00.123Z",
    "extra": {}
  },

  "regime": {
    "regime": "weak_bull",
    "risk_level": "NORMAL",
    "confidence": 0.82,
    "source": "S0",
    "timestamp": "2026-09-01T12:00:00.123Z",
    "extra": {}
  },

  "strategy": {
    "strategy": "S6",
    "version": "1",
    "score": 82.0,
    "entry_mode": "RIGHT_MOMENTUM",
    "timestamp": "2026-09-01T12:00:00.123Z",
    "extra": {}
  },

  "gates": [
    {"name": "fresh", "stage": 1, "passed": true, "value": true, "threshold": null,
     "reason": null, "timestamp": "2026-09-01T12:00:00.123Z", "duration_ms": null, "metadata": {}},
    {"name": "market_allowed", "stage": 2, "passed": true, "value": "weak_bull",
     "threshold": "not risk-off", "reason": null,
     "timestamp": "2026-09-01T12:00:00.123Z", "duration_ms": null, "metadata": {}},
    {"name": "trend", "stage": 3, "passed": true, "value": "UP", "threshold": "UP",
     "reason": null, "timestamp": "2026-09-01T12:00:00.123Z", "duration_ms": null, "metadata": {}},
    {"name": "atr", "stage": 4, "passed": false, "value": 7.2, "threshold": 6.0,
     "reason": "ATR exceeds maximum",
     "timestamp": "2026-09-01T12:00:00.123Z", "duration_ms": null, "metadata": {}}
  ],

  "decision": {
    "action": "REJECT",
    "accepted": false,
    "reason": "atr_gate_failed",
    "final_score": 82.0,
    "timestamp": "2026-09-01T12:00:00.123Z"
  },

  "risk": null,

  "metadata": {
    "environment": "REPLAY",
    "hostname": null,
    "process": "S6",
    "pid": null,
    "git_commit": null,
    "config_version": null,
    "correlation_id": "corr-001",
    "parent_signal_id": "evt-001",
    "extra": {}
  }
}
```

## 6. source 说明

| source | 含义 | 当前状态 |
|---|---|---|
| S3 | 引擎内置事件检测（s3_orderflow） | 当前唯一活跃信号源 |
| TRADINGVIEW | tv_bridge webhook | 已建，待 TV 付费套餐 |
| AI | AI 产生的信号（未来） | 预留 |
| MANUAL | 人工触发（未来调试/覆盖） | 预留 |
| REPLAY | 回放器重放的信号 | 预留（V2 Phase 1） |
| UNKNOWN | 未标注来源 | 默认值 |

模型层**不校验** source 是否在表内（只要求非空字符串），新来源零改动接入。

## 7. gate 说明

Gate 是决策链中"可以单独说'通过/不通过'"的判定点。命名建议（对齐现有 S6/S8 Gate 链）：

| name | 对应现有逻辑 |
|---|---|
| fresh | 事件新鲜度（event_is_stale） |
| cooldown | 信号/标的冷却（is_event_fresh、cooldowns） |
| strength | 信号强度门槛（如短空 raw_strength≥60） |
| regime | S0 市场状态门控（VIOLENT_BULLISH × regime 等） |
| position_limit | 仓位上限 / 恢复模式 |
| market_allowed | S0 risk_off 全局拦截 |
| trend | 1h EMA20 趋势过滤 |
| entry_mode | 左右侧分类（UNCONFIRMED 拒绝） |
| atr | ATR 波动率过滤 / 追高保护 |
| rr | R:R 预判 |
| score | strength≥30 / 分析过滤（_analysis_gate） |

以上仅为**命名约定**，模型层不校验 Gate 名与业务规则的对应关系。

## 8. versioning

- `journal_version` 当前为 `"1.0"`（`journal/models.py: JOURNAL_VERSION`，`schema.py` 同步）
- **兼容式变更**（加可选字段、加 extra 内容）→ 次版本号递增（1.1），旧读者可读
- **破坏式变更**（改字段含义、删字段、改枚举）→ 主版本号递增（2.0），新旧 Schema 并存
- 禁止用 `"v2"` 这类名称做版本号
- 未知字段的处理：序列化层**容忍**（from_dict 丢弃未知键），Schema 层**严格**（additionalProperties: false）——写侧演进靠 extra，读侧演进靠 version

## 9. backward compatibility

- Journal 是**新增能力**，不替换任何现有数据结构
- 不修改：S3 event schema、Redis event payload、TV webhook schema、PG/CH 表
- 未来接入时（P1-02+）：由 Adapter 在决策点组装 Journal，现有 Gate 函数**原样调用**，Journal 只在旁边记录

## 10. Decimal / datetime 序列化规则

| 类型 | 规则 |
|---|---|
| datetime | → UTC ISO-8601 字符串（毫秒，`Z` 后缀）。naive datetime 视为 UTC |
| Enum | → 其字符串值 |
| **Decimal** | → **字符串**（保留精度，绝不悄悄 float 化）。代价：round-trip 后该字段是 str，本文档显式声明 |
| tuple / set | → list（JSON 无 tuple 概念） |
| NaN / Infinity | 拒绝（`json.dumps(..., allow_nan=False)` 抛 ValueError），绝不产出非法 JSON |
| 未知对象 | `str()` 兜底，保证 to_json 永远合法 |

`to_json()` 使用 `sort_keys=True`：同一 Journal 的输出**字节级确定**（Replay diff 的前提）。

## 11. Storage（本阶段不负责）

本阶段只有 Model + Serializer + Schema + Tests。Journal 的落地位置
（JSONL 文件 / ClickHouse 新表 / 内存队列）由 **P1-02 Journal Writer** 决定。
选择 Writer 时的约束：写入必须 fire-and-forget（不阻塞交易主链路）、
失败只告警不影响下单、env 标签沿用现有 `get_env()`。
