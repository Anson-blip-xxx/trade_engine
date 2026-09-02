"""DecisionJournal 序列化测试：round-trip / 确定性 / JSON 安全。"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

import pytest
from factories import build_full_journal, build_minimal_journal

from journal import (
    DecisionAction,
    DecisionJournal,
    DecisionResult,
    GateResult,
    JournalValidationError,
    MarketSnapshot,
    Side,
    SignalSnapshot,
    from_dict,
    from_json,
    to_dict,
    to_json,
)


def test_round_trip_dict():
    """to_dict → from_dict 后与原对象一致。"""
    j = build_full_journal()
    restored = from_dict(to_dict(j))
    assert restored == j


def test_round_trip_json():
    """to_json → from_json 后与原对象一致，且再次序列化字节级一致。"""
    j = build_full_journal()
    text = to_json(j)
    restored = from_json(text)
    assert restored == j
    assert to_json(restored) == text


def test_minimal_round_trip():
    """最小 Journal 同样可以 round-trip。"""
    j = build_minimal_journal()
    assert from_json(to_json(j)) == j


def test_deterministic_serialization():
    """同一 Journal 多次 to_json 输出完全一致（sort_keys）。"""
    j = build_full_journal()
    assert to_json(j) == to_json(j) == to_json(j)
    assert json.loads(to_json(j)) == to_dict(j)


def test_nullable_market_fields_serialize():
    """null 字段序列化为 JSON null，round-trip 保持 None。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        market=MarketSnapshot(symbol="BTCUSDT", bid=None, ask=None, open_interest=None),
        decision=DecisionResult(),
    )
    d = to_dict(j)
    assert d["market"]["bid"] is None
    assert d["market"]["ask"] is None
    assert d["market"]["open_interest"] is None
    assert from_json(to_json(j)) == j


def test_json_safety_decimal():
    """Decimal → 字符串（保留精度，不悄悄 float 化）。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        market=MarketSnapshot(symbol="BTCUSDT", price=Decimal("112300.50000001")),
        decision=DecisionResult(),
    )
    text = to_json(j)
    assert "112300.50000001" in text  # 精度无损
    assert to_dict(j)["market"]["price"] == "112300.50000001"
    # 文档化的代价：Decimal 字段 round-trip 后是 str
    restored = from_json(text)
    assert restored.market.price == "112300.50000001"


def test_json_safety_datetime():
    """datetime → UTC ISO-8601 字符串。"""
    dt = datetime(2026, 9, 1, 12, 0, 0, 123000, tzinfo=timezone.utc)
    j = DecisionJournal(created_at=dt, decision_at=dt,
                        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
                        decision=DecisionResult())
    d = to_dict(j)
    assert d["created_at"] == "2026-09-01T12:00:00.123Z"


def test_json_safety_enum():
    """Enum → 字符串值。"""
    class FakeFutureEnum(Enum):
        V2_SOURCE = "FUTURE_FEED"

    j = DecisionJournal(
        signal=SignalSnapshot(source=FakeFutureEnum.V2_SOURCE, symbol="BTCUSDT", side=Side.LONG),
        decision=DecisionResult(action=DecisionAction.HOLD, accepted=False),
    )
    assert to_dict(j)["signal"]["source"] == "FUTURE_FEED"
    assert from_json(to_json(j)).signal.source == "FUTURE_FEED"


def test_json_safety_nested_containers():
    """dict / list 嵌套均产出合法 JSON；tuple 输入统一转为 list。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG,
                              raw={"tags": ["a", "b"], "nested": {"k": [1, 2, {"z": True}]}}),
        market=MarketSnapshot(symbol="BTCUSDT", extra={"levels": [1.1, 2.2]}),
        gates=(GateResult(name="trend", stage=1, passed=True,
                          value={"dir": "UP", "checks": [1, 2]}),),
        decision=DecisionResult(),
    )
    parsed = json.loads(to_json(j))  # 能 parse 即合法 JSON
    assert parsed["signal"]["raw"]["tags"] == ["a", "b"]
    assert parsed["gates"][0]["value"]["checks"] == [1, 2]
    assert from_json(to_json(j)) == j


def test_tuple_input_serialized_as_list():
    """tuple 入参序列化为 list（round-trip 后为 list，JSON 无 tuple 概念）。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG,
                              raw={"pair": ("a", 1)}),
        decision=DecisionResult(),
    )
    assert to_dict(j)["signal"]["raw"]["pair"] == ["a", 1]
    restored = from_json(to_json(j))
    assert restored.signal.raw["pair"] == ["a", 1]


def test_json_rejects_nan():
    """NaN 会被拒绝（allow_nan=False），绝不产出非法 JSON。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        market=MarketSnapshot(symbol="BTCUSDT", price=float("nan")),
        decision=DecisionResult(),
    )
    with pytest.raises(ValueError):
        to_json(j)


def test_unknown_extra_fields_round_trip():
    """extra 中的未知字段参与序列化并完整 round-trip。"""
    from journal import Metadata
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        metadata=Metadata(extra={"future_block": {"anything": [1, "x", None]}}),
        decision=DecisionResult(),
    )
    restored = from_json(to_json(j))
    assert restored == j
    assert restored.metadata.extra["future_block"] == {"anything": [1, "x", None]}


def test_from_dict_ignores_unknown_keys():
    """from_dict 容忍未知键（向前兼容：旧读者读新数据不崩）。"""
    j = build_minimal_journal()
    d = to_dict(j)
    d["future_top_level_field"] = {"x": 1}
    d["signal"]["future_signal_field"] = 123
    restored = from_dict(d)
    assert restored == j


def test_from_dict_invalid_raises():
    """非法 Journal（空 version / 空 symbol / 坏 side）必须抛 JournalValidationError。"""
    base = to_dict(build_minimal_journal())

    bad_version = dict(base, journal_version="")
    with pytest.raises(JournalValidationError):
        from_dict(bad_version)

    bad_symbol = dict(base)
    bad_symbol["signal"] = dict(base["signal"], symbol="")
    with pytest.raises(JournalValidationError):
        from_dict(bad_symbol)

    bad_side = dict(base)
    bad_side["signal"] = dict(base["signal"], side="BUY")
    with pytest.raises(JournalValidationError):
        from_dict(bad_side)

    bad_gates = dict(base, gates=[{"name": "a", "stage": 5, "passed": True}])
    with pytest.raises(JournalValidationError):
        from_dict(bad_gates)


def test_from_json_bad_text_raises():
    """非法 JSON 文本抛错（json 层错误原样上抛）。"""
    with pytest.raises(json.JSONDecodeError):
        from_json("not-json")


def test_side_normalization_in_signal():
    """signal.side / symbol 大小写归一化。"""
    j = DecisionJournal(
        signal=SignalSnapshot(symbol="btcusdt", side="long"),
        decision=DecisionResult(),
    )
    assert j.signal.symbol == "BTCUSDT"
    assert j.signal.side == "LONG"
