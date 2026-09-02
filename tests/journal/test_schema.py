"""Decision Journal JSON Schema 测试：Draft 2020-12 结构与模型一致性。

项目未引入 jsonschema 依赖，本文件做结构一致性校验
（Schema 字段与 dataclass 字段保持同步，防止两者漂移）。
"""
import dataclasses

from factories import build_full_journal

from journal import (
    DECISION_JOURNAL_SCHEMA,
    DECISION_JOURNAL_SCHEMA_VERSION,
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    RiskSnapshot,
    SignalSnapshot,
    StrategySnapshot,
    to_dict,
)

_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def test_schema_declares_draft_2020_12():
    assert DECISION_JOURNAL_SCHEMA["$schema"] == _DRAFT
    assert DECISION_JOURNAL_SCHEMA["title"] == "DecisionJournal"


def test_schema_version_constant():
    """Schema 版本与模型 JOURNAL_VERSION 一致（当前 1.0）。"""
    from journal import JOURNAL_VERSION
    assert DECISION_JOURNAL_SCHEMA_VERSION == JOURNAL_VERSION == "1.0"


def test_schema_root_required_keys():
    assert set(DECISION_JOURNAL_SCHEMA["required"]) == {
        "journal_version", "journal_id", "created_at", "decision_at", "signal", "decision",
    }
    assert DECISION_JOURNAL_SCHEMA["additionalProperties"] is False


def test_schema_root_properties_match_model():
    """根 properties 与 DecisionJournal 字段一一对应。"""
    model_fields = {f.name for f in dataclasses.fields(DecisionJournal)}
    schema_props = set(DECISION_JOURNAL_SCHEMA["properties"].keys())
    assert schema_props == model_fields


def test_schema_defs_match_snapshot_models():
    """每个 $defs 快照的 properties 与对应 dataclass 字段一一对应。"""
    pairs = {
        "SignalSnapshot": SignalSnapshot,
        "MarketSnapshot": MarketSnapshot,
        "RegimeSnapshot": RegimeSnapshot,
        "StrategySnapshot": StrategySnapshot,
        "GateResult": GateResult,
        "DecisionResult": DecisionResult,
        "RiskSnapshot": RiskSnapshot,
        "Metadata": Metadata,
    }
    for name, cls in pairs.items():
        schema_props = set(DECISION_JOURNAL_SCHEMA["$defs"][name]["properties"].keys())
        model_fields = {f.name for f in dataclasses.fields(cls)}
        assert schema_props == model_fields, f"{name} schema/model 字段漂移"


def test_gate_result_schema_rules():
    gate = DECISION_JOURNAL_SCHEMA["$defs"]["GateResult"]
    assert set(gate["required"]) == {"name", "stage", "passed"}
    assert gate["properties"]["stage"]["minimum"] == 1
    assert gate["properties"]["stage"]["type"] == "integer"


def test_side_enum_matches_model():
    """schema 中 side 枚举与模型 SIDES 一致。"""
    from journal import SIDES
    enum = DECISION_JOURNAL_SCHEMA["$defs"]["SignalSnapshot"]["properties"]["side"]["enum"]
    assert tuple(enum) == SIDES


def test_action_enum_matches_model():
    from journal import ACTIONS
    enum = DECISION_JOURNAL_SCHEMA["$defs"]["DecisionResult"]["properties"]["action"]["enum"]
    assert tuple(enum) == ACTIONS


def test_full_journal_satisfies_schema_shape():
    """完整 Journal 的 to_dict 输出满足 schema 的根 required 与快照 required。"""
    d = to_dict(build_full_journal())
    for key in DECISION_JOURNAL_SCHEMA["required"]:
        assert key in d, f"缺少根必须字段 {key}"

    # journal 字段名 → $defs 类名
    sections = {
        "signal": "SignalSnapshot",
        "market": "MarketSnapshot",
        "regime": "RegimeSnapshot",
        "strategy": "StrategySnapshot",
        "decision": "DecisionResult",
        "metadata": "Metadata",
    }
    for field_name, def_name in sections.items():
        required = DECISION_JOURNAL_SCHEMA["$defs"][def_name].get("required", [])
        for key in required:
            assert key in d[field_name], f"{field_name} 缺少必须字段 {key}"

    for g in d["gates"]:
        for key in DECISION_JOURNAL_SCHEMA["$defs"]["GateResult"]["required"]:
            assert key in g
