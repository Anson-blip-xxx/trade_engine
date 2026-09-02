# journal/ — Decision Journal

交易决策链的结构化行为记录（V2 P1-01）。独立 Domain/Infrastructure，不依赖、不被依赖于任何交易模块。

- `models.py` — 9 个 frozen dataclass + 结构校验（`validate_journal`）
- `serializer.py` — `to_dict / to_json / from_dict / from_json`
- `schema.py` — JSON Schema Draft 2020-12（`DECISION_JOURNAL_SCHEMA`）
- 详细字段说明与示例见 `docs/v2/DECISION_JOURNAL_SCHEMA.md`

## 最小用法

```python
from journal import (
    DecisionJournal, SignalSnapshot, GateResult, DecisionResult,
    DecisionAction, Side, to_json, from_json, validate_journal,
)

journal = DecisionJournal(
    signal=SignalSnapshot(source="S3", signal_type="PULSE_UP",
                          symbol="BTCUSDT", side=Side.LONG, strength=78),
    gates=(
        GateResult(name="fresh", stage=1, passed=True, value=True),
        GateResult(name="atr", stage=2, passed=False, value=7.2, threshold=6.0,
                   reason="ATR exceeds maximum"),
    ),
    decision=DecisionResult(action=DecisionAction.REJECT, accepted=False,
                            reason="atr_gate_failed"),
)

assert validate_journal(journal) == []

text = to_json(journal)     # 确定性 JSON（sort_keys，NaN 拒绝）
same = from_json(text)      # 回读，与原对象相等（round-trip）
```

## 约束

- **Immutable**：全部 `@dataclass(frozen=True)`，创建后不可改
- **Source-agnostic**：`source` 接受任意字符串（S3 / TRADINGVIEW / AI / MANUAL / REPLAY / ...）
- **不落地**：本包不写 Redis / DB / 文件（Writer 属 P1-02）
- **不接入交易链**：S3/S0/S6/S8/PM/shared_executor 当前不 import 本包
