"""Decision Journal — 交易决策链的结构化行为记录（V2 P1-01）。

只提供 数据模型 + 序列化 + JSON Schema，不负责存储（P1-02 再定 Writer），
不接入任何交易链路（S3/S0/S6/S8/PM/shared_executor 保持零感知）。

最小示例::

    from journal import (
        DecisionJournal, SignalSnapshot, GateResult, DecisionResult,
        DecisionAction, Side, to_json, from_json,
    )

    journal = DecisionJournal(
        signal=SignalSnapshot(source="S3", signal_type="PULSE_UP",
                              symbol="BTCUSDT", side=Side.LONG, strength=78),
        gates=(GateResult(name="fresh", stage=1, passed=True),),
        decision=DecisionResult(action=DecisionAction.OPEN, accepted=True, final_score=78),
    )

    text = to_json(journal)      # 确定性 JSON
    again = from_json(text)      # 回读，与原对象相等
"""
from journal.builder import DecisionJournalBuilder, signal_source_from_event
from journal.models import (
    ACTIONS,
    JOURNAL_VERSION,
    KNOWN_SOURCES,
    SIDES,
    DecisionAction,
    DecisionJournal,
    DecisionResult,
    GateResult,
    JournalValidationError,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    RiskSnapshot,
    Side,
    SignalSnapshot,
    StrategySnapshot,
    new_journal_id,
    utc_now_iso,
    validate_journal,
)
from journal.recorder import (
    JournalRecorder,
    MemoryJournalRecorder,
    NullJournalRecorder,
    get_default_recorder,
    safe_record,
    set_default_recorder,
)
from journal.schema import DECISION_JOURNAL_SCHEMA, DECISION_JOURNAL_SCHEMA_VERSION
from journal.serializer import from_dict, from_json, to_dict, to_json

__all__ = [
    "ACTIONS",
    "DECISION_JOURNAL_SCHEMA",
    "DECISION_JOURNAL_SCHEMA_VERSION",
    "JOURNAL_VERSION",
    "KNOWN_SOURCES",
    "SIDES",
    "DecisionAction",
    "DecisionJournal",
    "DecisionJournalBuilder",
    "DecisionResult",
    "GateResult",
    "JournalRecorder",
    "JournalValidationError",
    "MarketSnapshot",
    "MemoryJournalRecorder",
    "Metadata",
    "NullJournalRecorder",
    "RegimeSnapshot",
    "RiskSnapshot",
    "Side",
    "SignalSnapshot",
    "StrategySnapshot",
    "from_dict",
    "from_json",
    "get_default_recorder",
    "new_journal_id",
    "safe_record",
    "set_default_recorder",
    "signal_source_from_event",
    "to_dict",
    "to_json",
    "utc_now_iso",
    "validate_journal",
]
