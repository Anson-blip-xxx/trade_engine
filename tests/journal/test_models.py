"""DecisionJournal 模型测试：构造 / 最小合法 / 不可变 / Gate 顺序 / 校验。"""
import dataclasses
from datetime import datetime, timezone

import pytest
from factories import T, build_full_journal, build_minimal_journal

from journal import (
    JOURNAL_VERSION,
    DecisionAction,
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    Side,
    SignalSnapshot,
    new_journal_id,
    utc_now_iso,
    validate_journal,
)


def test_full_journal_construction():
    """完整 Journal：所有字段按预期落位。"""
    j = build_full_journal()

    assert j.journal_version == "1.0"
    assert j.journal_id == "0f0e1d2c-3b4a-4958-8677-112233445566"
    assert j.created_at == T and j.decision_at == T

    assert j.signal.source == "S3"
    assert j.signal.signal_type == "PULSE_UP"
    assert j.signal.symbol == "BTCUSDT"
    assert j.signal.side == "LONG"
    assert j.signal.strength == 78
    assert j.signal.event_id == "evt-001"
    assert j.signal.raw == {"type": "PULSE_UP", "chg_15m": 3.2}

    assert j.market.price == 112300.5
    assert j.market.taker_buy_ratio == 0.57
    assert j.market.open_interest is None

    assert j.regime.regime == "weak_bull"
    assert j.regime.confidence == 0.82

    assert j.strategy.strategy == "S6"
    assert j.strategy.score == 82
    assert j.strategy.entry_mode == "RIGHT_MOMENTUM"

    assert [g.stage for g in j.gates] == [1, 2, 3, 4]
    assert [g.passed for g in j.gates] == [True, True, True, False]
    assert j.gates[3].reason == "ATR exceeds maximum"

    assert j.decision.action == "REJECT"
    assert j.decision.accepted is False
    assert j.decision.reason == "atr_gate_failed"

    assert j.risk is None
    assert j.metadata.environment == "REPLAY"
    assert j.metadata.correlation_id == "corr-001"
    assert validate_journal(j) == []


def test_minimal_journal_valid():
    """最小合法 Journal：只提供必须字段即可创建并通过校验。"""
    j = build_minimal_journal()
    assert j.signal.symbol == "BTCUSDT"
    assert j.signal.side == "LONG"
    assert j.decision.action == "REJECT"
    assert j.gates == ()
    assert j.risk is None
    assert j.journal_version == JOURNAL_VERSION
    assert validate_journal(j) == []


def test_defaults_generated():
    """未提供 id/时间时自动生成，且两次创建得到不同 id。"""
    j1 = build_minimal_journal()
    j2 = build_minimal_journal()
    assert j1.journal_id and j2.journal_id and j1.journal_id != j2.journal_id
    assert j1.created_at.endswith("Z")
    assert validate_journal(j1) == []


def test_immutability_frozen():
    """创建后不可修改核心字段（frozen dataclass）。"""
    j = build_full_journal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.journal_id = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.signal = SignalSnapshot(symbol="ETHUSDT", side=Side.SHORT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.decision = DecisionResult(action=DecisionAction.OPEN, accepted=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.gates = ()
    # 嵌套快照同样 frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.signal.strength = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        j.gates[0].passed = False


def test_defensive_copy_of_dict_fields():
    """构造时对外部 dict 做防御性拷贝：事后改原 dict 不影响 Journal。"""
    raw = {"k": 1}
    extra = {"x": 1}
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG, raw=raw),
        market=MarketSnapshot(symbol="BTCUSDT", extra=extra),
    )
    raw["k"] = 999
    extra["x"] = 999
    assert j.signal.raw == {"k": 1}
    assert j.market.extra == {"x": 1}


def test_gate_ordering_valid():
    """合法顺序 1,2,3,4 必须通过。"""
    gates = tuple(GateResult(name=f"g{i}", stage=i, passed=True) for i in range(1, 5))
    j = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        gates=gates, decision=DecisionResult())
    assert validate_journal(j) == []


def test_gate_ordering_gap_invalid():
    """非法：1,3,4（缺 2）必须失败。"""
    gates = tuple(GateResult(name=f"g{i}", stage=i, passed=True) for i in (1, 3, 4))
    j = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        gates=gates, decision=DecisionResult())
    errors = validate_journal(j)
    assert any("连续" in e for e in errors)


def test_gate_ordering_duplicate_invalid():
    """非法：1,2,2（重复）必须失败。"""
    gates = (
        GateResult(name="a", stage=1, passed=True),
        GateResult(name="b", stage=2, passed=True),
        GateResult(name="c", stage=2, passed=True),
    )
    j = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        gates=gates, decision=DecisionResult())
    errors = validate_journal(j)
    assert any("重复" in e for e in errors)


def test_gate_ordering_not_starting_at_one_invalid():
    """非法：2,3（未从 1 开始）必须失败。"""
    gates = tuple(GateResult(name=f"g{i}", stage=i, passed=True) for i in (2, 3))
    j = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        gates=gates, decision=DecisionResult())
    errors = validate_journal(j)
    assert any("从 1 开始" in e for e in errors)


def test_gate_rejection_recorded():
    """Gate1 通过 / Gate2 失败 → Decision REJECT 并记录 reason（Journal 仍合法）。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        gates=(
            GateResult(name="fresh", stage=1, passed=True, value=True),
            GateResult(name="atr", stage=2, passed=False, value=7.2, threshold=6.0,
                       reason="ATR exceeds maximum"),
        ),
        decision=DecisionResult(action=DecisionAction.REJECT, accepted=False,
                                reason="atr_gate_failed"),
    )
    assert validate_journal(j) == []
    assert j.gates[1].passed is False
    assert j.decision.action == "REJECT"
    assert j.decision.accepted is False


def test_nullable_market_fields():
    """bid/ask/open_interest 等为 null 时正常构造（序列化由 serializer 测试覆盖）。"""
    m = MarketSnapshot(symbol="BTCUSDT", bid=None, ask=None, open_interest=None)
    j = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        market=m, decision=DecisionResult())
    assert validate_journal(j) == []
    assert j.market.bid is None and j.market.ask is None


def test_future_sources_not_restricted():
    """模型不限制 source：5 个文档来源 + 任意未来来源都必须可用。"""
    for source in ("S3", "TRADINGVIEW", "AI", "MANUAL", "REPLAY", "SOME_FUTURE_SOURCE"):
        j = DecisionJournal(
            signal=SignalSnapshot(source=source, symbol="BTCUSDT", side=Side.LONG),
            decision=DecisionResult(),
        )
        assert validate_journal(j) == []
        assert j.signal.source == source


def test_schema_version_validation():
    """journal_version='1.0' 合法；空串必须失败。"""
    ok = DecisionJournal(journal_version="1.0",
                         signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                         decision=DecisionResult())
    assert validate_journal(ok) == []

    bad = DecisionJournal(journal_version="",
                          signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                          decision=DecisionResult())
    errors = validate_journal(bad)
    assert any("journal_version" in e for e in errors)


def test_side_and_action_validation():
    """非法 side / action 必须报错。"""
    bad_side = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side="BUY"),
                               decision=DecisionResult())
    assert any("signal.side" in e for e in validate_journal(bad_side))

    bad_action = DecisionJournal(signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                                 decision=DecisionResult(action="BUY", accepted=True))
    assert any("decision.action" in e for e in validate_journal(bad_action))


def test_enum_inputs_coerced():
    """接受 Enum 入参（Side/DecisionAction），统一为字符串值。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        decision=DecisionResult(action=DecisionAction.OPEN, accepted=True),
    )
    assert j.signal.side == "LONG"
    assert j.decision.action == "OPEN"


def test_datetime_inputs_coerced():
    """datetime 入参自动转为 UTC ISO-8601 字符串。"""
    dt = datetime(2026, 9, 1, 12, 0, 0, 123000, tzinfo=timezone.utc)
    j = DecisionJournal(created_at=dt, decision_at=dt,
                        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG,
                                              signal_timestamp=dt),
                        decision=DecisionResult())
    assert j.created_at == "2026-09-01T12:00:00.123Z"
    assert j.decision_at == j.created_at
    assert j.signal.signal_timestamp == "2026-09-01T12:00:00.123Z"


def test_unknown_extra_fields_allowed():
    """extra 中的未知字段正常工作（向前兼容）。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        market=MarketSnapshot(symbol="BTCUSDT", extra={"orderbook_imbalance": 0.3}),
        regime=RegimeSnapshot(regime="range", extra={"sentiment_v2": {"score": 0.7}}),
        metadata=Metadata(extra={"custom_tag": "abc"}),
        decision=DecisionResult(),
    )
    assert validate_journal(j) == []
    assert j.market.extra["orderbook_imbalance"] == 0.3
    assert j.metadata.extra["custom_tag"] == "abc"


def test_helpers():
    """new_journal_id 唯一；utc_now_iso 为 Z 结尾 UTC 字符串。"""
    assert new_journal_id() != new_journal_id()
    now = utc_now_iso()
    assert now.endswith("Z") and "T" in now
