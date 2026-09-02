"""Decision Journal JSON Schema（Draft 2020-12）。

本模块只提供 Schema 字典；项目未引入 jsonschema 依赖，
结构校验由 journal.models.validate_journal 承担，Schema 作为对外契约文档。
"""
from __future__ import annotations

from journal.models import JOURNAL_VERSION

__all__ = ["DECISION_JOURNAL_SCHEMA"]

_ISO_TS = {
    "type": "string",
    "description": "UTC ISO-8601，例如 2026-09-01T12:00:00.123Z",
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$",
}

_NUM = {"type": ["number", "null"]}
_OPT_STR = {"type": ["string", "null"]}
_OBJ = {"type": "object", "additionalProperties": True}

DECISION_JOURNAL_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://trade-engine.local/schemas/decision-journal-1.0.json",
    "title": "DecisionJournal",
    "description": "一次交易信号经过策略决策链的完整不可变记录（P1-01）。"
                   "扩展一律通过各快照的 extra 字段；新增顶层字段必须提升 journal_version。",
    "type": "object",
    "additionalProperties": False,
    "required": ["journal_version", "journal_id", "created_at", "decision_at", "signal", "decision"],
    "properties": {
        "journal_version": {"type": "string", "minLength": 1,
                            "description": "Schema 版本，如 1.0"},
        "journal_id": {"type": "string", "minLength": 1,
                       "description": "全局唯一 ID（UUID，不依赖数据库自增）"},
        "created_at": {**_ISO_TS, "description": "Journal 创建时间（UTC）"},
        "decision_at": {**_ISO_TS, "description": "决策实际发生时间（UTC）"},
        "signal": {"$ref": "#/$defs/SignalSnapshot"},
        "market": {"$ref": "#/$defs/MarketSnapshot"},
        "regime": {"$ref": "#/$defs/RegimeSnapshot"},
        "strategy": {"$ref": "#/$defs/StrategySnapshot"},
        "gates": {
            "type": "array",
            "description": "Gate 判定结果，按执行顺序排列（stage 从 1 开始且连续）",
            "items": {"$ref": "#/$defs/GateResult"},
        },
        "decision": {"$ref": "#/$defs/DecisionResult"},
        "risk": {
            "description": "Risk 层输出；信号在 Risk 之前被拒时为 null",
            "oneOf": [{"$ref": "#/$defs/RiskSnapshot"}, {"type": "null"}],
        },
        "metadata": {"$ref": "#/$defs/Metadata"},
    },
    "$defs": {
        "SignalSnapshot": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source", "signal_type", "symbol", "side"],
            "properties": {
                "source": {"type": "string", "minLength": 1,
                           "description": "信号来源；文档化取值 S3/TRADINGVIEW/AI/MANUAL/REPLAY/UNKNOWN，"
                                          "模型层不做硬限制（source-agnostic）"},
                "signal_type": {"type": "string",
                                "description": "事件类型，如 PULSE_UP / TREND_DOWN；不在模型层硬编码全集"},
                "symbol": {"type": "string", "minLength": 1, "description": "如 BTCUSDT"},
                "side": {"type": "string", "enum": ["LONG", "SHORT", "NEUTRAL", "UNKNOWN"]},
                "strength": {**_NUM, "description": "信号强度原始值（当前系统 0~100），不做换算"},
                "signal_timestamp": _ISO_TS,
                "event_id": {**_OPT_STR, "description": "事件去重/关联 ID"},
                "raw": {**_OBJ, "description": "原始 signal payload（必须 JSON 可序列化）"},
            },
        },
        "MarketSnapshot": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "symbol": {"type": "string"},
                "price": _NUM,
                "bid": _NUM,
                "ask": _NUM,
                "spread": _NUM,
                "volume": _NUM,
                "volume_ratio": _NUM,
                "atr": _NUM,
                "atr_pct": _NUM,
                "ema20": _NUM,
                "ema50": _NUM,
                "rsi": _NUM,
                "taker_buy_ratio": _NUM,
                "open_interest": _NUM,
                "funding_rate": _NUM,
                "timestamp": _ISO_TS,
                "extra": {**_OBJ, "description": "未来扩展字段（orderbook 等）"},
            },
        },
        "RegimeSnapshot": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "regime": {**_OPT_STR, "description": "如 weak_bull；不做枚举硬限制"},
                "risk_level": {**_OPT_STR, "description": "LOW/NORMAL/HIGH/CRITICAL"},
                "confidence": {**_NUM, "description": "0~1；当前 S0 无此值则为 null"},
                "source": _OPT_STR,
                "timestamp": _ISO_TS,
                "extra": _OBJ,
            },
        },
        "StrategySnapshot": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "strategy": _OPT_STR,
                "version": _OPT_STR,
                "score": {**_NUM, "description": "策略评分原始值，不做范围假设"},
                "entry_mode": _OPT_STR,
                "timestamp": _ISO_TS,
                "extra": _OBJ,
            },
        },
        "GateResult": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "stage", "passed"],
            "properties": {
                "name": {"type": "string", "minLength": 1,
                         "description": "Gate 名（小写），如 fresh/cooldown/trend/atr/score"},
                "stage": {"type": "integer", "minimum": 1, "description": "执行顺序，从 1 开始且连续"},
                "passed": {"type": "boolean"},
                "value": {"description": "Gate 实际取值（任意 JSON 值）"},
                "threshold": {"description": "Gate 阈值（任意 JSON 值或 null）"},
                "reason": {**_OPT_STR, "description": "失败原因；失败时必须尽量提供"},
                "timestamp": _ISO_TS,
                "duration_ms": {"type": ["number", "null"], "description": "本阶段允许 null（不强制采集）"},
                "metadata": _OBJ,
            },
        },
        "DecisionResult": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action", "accepted"],
            "properties": {
                "action": {"type": "string",
                           "enum": ["OPEN", "REJECT", "HOLD", "CLOSE", "NO_ACTION"]},
                "accepted": {"type": "boolean"},
                "reason": _OPT_STR,
                "final_score": _NUM,
                "timestamp": _ISO_TS,
            },
        },
        "RiskSnapshot": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "risk_level": _OPT_STR,
                "position_size": _NUM,
                "position_fraction": _NUM,
                "leverage": {"type": ["integer", "null"]},
                "max_loss": _NUM,
                "stop_pct": _NUM,
                "take_profit_pct": _NUM,
                "timestamp": _ISO_TS,
                "metadata": _OBJ,
            },
        },
        "Metadata": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "environment": {**_OPT_STR, "description": "SANDBOX/PAPER/LIVE/REPLAY"},
                "hostname": _OPT_STR,
                "process": _OPT_STR,
                "pid": {"type": ["integer", "null"]},
                "git_commit": {**_OPT_STR, "description": "由调用方提供"},
                "config_version": _OPT_STR,
                "correlation_id": {**_OPT_STR, "description": "Signal→Decision→Order→Position 串联"},
                "parent_signal_id": _OPT_STR,
                "extra": _OBJ,
            },
        },
    },
}

DECISION_JOURNAL_SCHEMA_VERSION = JOURNAL_VERSION
