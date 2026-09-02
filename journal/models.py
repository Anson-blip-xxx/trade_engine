"""Decision Journal 数据模型（P1-01）。

职责边界：
- 只"记录"决策链当时看到的输入与判定结果（Signal/Market/Regime/Gate/Decision/Risk）
- 不做任何交易计算、不取行情、不落库、不接 Redis
- 全部对象 frozen：创建后不可变

Schema 版本策略见 docs/v2/DECISION_JOURNAL_SCHEMA.md。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Schema 版本：数字递增（1.0 → 1.1 → 2.0），不使用 "v2" 这类名称
JOURNAL_VERSION = "1.0"

# side 允许值：描述交易方向，而非订单动作（不用 BUY/SELL）
SIDES = ("LONG", "SHORT", "NEUTRAL", "UNKNOWN")

# decision.action 允许值
ACTIONS = ("OPEN", "REJECT", "HOLD", "CLOSE", "NO_ACTION")

# signal.source 文档化取值；模型层不做硬限制（source-agnostic）
SOURCE_S3 = "S3"
SOURCE_TRADINGVIEW = "TRADINGVIEW"
SOURCE_AI = "AI"
SOURCE_MANUAL = "MANUAL"
SOURCE_REPLAY = "REPLAY"
SOURCE_UNKNOWN = "UNKNOWN"
KNOWN_SOURCES = (SOURCE_S3, SOURCE_TRADINGVIEW, SOURCE_AI, SOURCE_MANUAL, SOURCE_REPLAY, SOURCE_UNKNOWN)


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class DecisionAction(str, Enum):
    OPEN = "OPEN"
    REJECT = "REJECT"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    NO_ACTION = "NO_ACTION"


class JournalValidationError(ValueError):
    """Journal 结构校验失败（仅结构校验，不含任何交易业务规则）。"""


def _enum_value(value: Any) -> Any:
    """str-Enum → 其字符串值；其他原样返回。"""
    return value.value if isinstance(value, Enum) else value


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（毫秒精度，Z 后缀）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_utc(value: str | datetime) -> str:
    """datetime → UTC ISO-8601 字符串；字符串原样返回。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return value


def new_journal_id() -> str:
    """生成唯一 journal_id（UUID4，不依赖数据库自增）。"""
    return str(uuid.uuid4())


def _is_valid_iso(ts: Any) -> bool:
    if not isinstance(ts, str) or not ts:
        return False
    try:
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _as_str(value: Any, default: str = "") -> str:
    v = _enum_value(value)
    return str(v) if v is not None else default


def _as_opt_str(value: Any) -> str | None:
    v = _enum_value(value)
    return None if v is None else str(v)


def _copy_dict(value: dict | None) -> dict:
    return dict(value) if value else {}


@dataclass(frozen=True)
class SignalSnapshot:
    """决策时刻的信号输入快照。source-agnostic：不假设一定来自 S3。"""

    source: str = SOURCE_UNKNOWN
    signal_type: str = ""
    symbol: str = ""
    side: str = Side.UNKNOWN.value
    strength: float | None = None
    signal_timestamp: str | None = None
    event_id: str | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "source", _as_str(self.source, SOURCE_UNKNOWN).upper())
        object.__setattr__(self, "signal_type", _as_str(self.signal_type).upper())
        object.__setattr__(self, "symbol", _as_str(self.symbol).upper())
        object.__setattr__(self, "side", _as_str(self.side, Side.UNKNOWN.value).upper())
        if self.strength is not None:
            object.__setattr__(self, "strength", float(self.strength))
        object.__setattr__(self, "signal_timestamp", _as_opt_str(iso_utc(self.signal_timestamp)) if self.signal_timestamp is not None else None)
        object.__setattr__(self, "event_id", _as_opt_str(self.event_id))
        object.__setattr__(self, "raw", _copy_dict(self.raw))


@dataclass(frozen=True)
class MarketSnapshot:
    """决策时刻的行情快照：只记录"当时系统看到什么"，不在模型内计算指标。"""

    symbol: str = ""
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    spread: float | None = None
    volume: float | None = None
    volume_ratio: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    rsi: float | None = None
    taker_buy_ratio: float | None = None
    open_interest: float | None = None
    funding_rate: float | None = None
    timestamp: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "symbol", _as_str(self.symbol).upper())
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)
        object.__setattr__(self, "extra", _copy_dict(self.extra))


@dataclass(frozen=True)
class RegimeSnapshot:
    """决策时刻的市场状态（S0 regime 等）。regime 不做枚举硬限制。"""

    regime: str | None = None
    risk_level: str | None = None
    confidence: float | None = None
    source: str | None = None
    timestamp: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "regime", _as_opt_str(self.regime))
        object.__setattr__(self, "risk_level", _as_opt_str(self.risk_level))
        object.__setattr__(self, "source", _as_opt_str(self.source))
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)
        object.__setattr__(self, "extra", _copy_dict(self.extra))


@dataclass(frozen=True)
class StrategySnapshot:
    """产生本次决策的策略上下文（策略名/版本/评分/进场模式）。"""

    strategy: str | None = None
    version: str | None = None
    score: float | None = None
    entry_mode: str | None = None
    timestamp: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "strategy", _as_opt_str(self.strategy))
        object.__setattr__(self, "version", _as_opt_str(self.version))
        object.__setattr__(self, "entry_mode", _as_opt_str(self.entry_mode))
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)
        object.__setattr__(self, "extra", _copy_dict(self.extra))


@dataclass(frozen=True)
class GateResult:
    """单个 Gate 的判定结果。gates 列表必须保持执行顺序（stage 1..N 连续）。"""

    name: str = ""
    stage: int = 0
    passed: bool = False
    value: Any = None
    threshold: Any = None
    reason: str | None = None
    timestamp: str | None = None
    duration_ms: float | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "name", _as_str(self.name).lower())
        object.__setattr__(self, "stage", int(self.stage))
        object.__setattr__(self, "passed", bool(self.passed))
        object.__setattr__(self, "reason", _as_opt_str(self.reason))
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)
        object.__setattr__(self, "metadata", _copy_dict(self.metadata))


@dataclass(frozen=True)
class DecisionResult:
    """决策链最终结论。"""

    action: str = DecisionAction.NO_ACTION.value
    accepted: bool = False
    reason: str | None = None
    final_score: float | None = None
    timestamp: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "action", _as_str(self.action, DecisionAction.NO_ACTION.value).upper())
        object.__setattr__(self, "accepted", bool(self.accepted))
        object.__setattr__(self, "reason", _as_opt_str(self.reason))
        if self.final_score is not None:
            object.__setattr__(self, "final_score", float(self.final_score))
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)


@dataclass(frozen=True)
class RiskSnapshot:
    """Risk 层输出快照。只记录，不重算；信号可能在 Risk 之前被拒（此时为 null）。"""

    risk_level: str | None = None
    position_size: float | None = None
    position_fraction: float | None = None
    leverage: int | None = None
    max_loss: float | None = None
    stop_pct: float | None = None
    take_profit_pct: float | None = None
    timestamp: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "risk_level", _as_opt_str(self.risk_level))
        if self.leverage is not None:
            object.__setattr__(self, "leverage", int(self.leverage))
        object.__setattr__(self, "timestamp", _as_opt_str(iso_utc(self.timestamp)) if self.timestamp is not None else None)
        object.__setattr__(self, "metadata", _copy_dict(self.metadata))


@dataclass(frozen=True)
class Metadata:
    """Journal 元信息：环境、进程、版本与关联 ID。"""

    environment: str | None = None   # SANDBOX / PAPER / LIVE / REPLAY（不硬限制）
    hostname: str | None = None
    process: str | None = None
    pid: int | None = None
    git_commit: str | None = None    # 由调用方提供，本模块不读 Git
    config_version: str | None = None
    correlation_id: str | None = None   # Signal → Decision → Order → Position 串联
    parent_signal_id: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "environment", _as_opt_str(self.environment))
        object.__setattr__(self, "hostname", _as_opt_str(self.hostname))
        object.__setattr__(self, "process", _as_opt_str(self.process))
        if self.pid is not None:
            object.__setattr__(self, "pid", int(self.pid))
        object.__setattr__(self, "git_commit", _as_opt_str(self.git_commit))
        object.__setattr__(self, "config_version", _as_opt_str(self.config_version))
        object.__setattr__(self, "correlation_id", _as_opt_str(self.correlation_id))
        object.__setattr__(self, "parent_signal_id", _as_opt_str(self.parent_signal_id))
        object.__setattr__(self, "extra", _copy_dict(self.extra))


@dataclass(frozen=True)
class DecisionJournal:
    """决策日志根对象：一次信号决策的完整、不可变记录。"""

    journal_version: str = JOURNAL_VERSION
    journal_id: str = field(default_factory=new_journal_id)
    created_at: str = field(default_factory=utc_now_iso)
    decision_at: str = field(default_factory=utc_now_iso)
    signal: SignalSnapshot = field(default_factory=SignalSnapshot)
    market: MarketSnapshot = field(default_factory=MarketSnapshot)
    regime: RegimeSnapshot = field(default_factory=RegimeSnapshot)
    strategy: StrategySnapshot = field(default_factory=StrategySnapshot)
    gates: tuple[GateResult, ...] = ()
    decision: DecisionResult = field(default_factory=DecisionResult)
    risk: RiskSnapshot | None = None
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self):
        object.__setattr__(self, "journal_version", _as_str(self.journal_version, JOURNAL_VERSION))
        object.__setattr__(self, "journal_id", _as_str(self.journal_id))
        object.__setattr__(self, "created_at", iso_utc(self.created_at))
        object.__setattr__(self, "decision_at", iso_utc(self.decision_at))
        object.__setattr__(self, "gates", tuple(self.gates or ()))


def validate_journal(journal: DecisionJournal) -> list[str]:
    """结构校验，返回错误列表（空列表 = 合法）。

    只校验 Journal 自身的结构完整性，不含任何交易业务规则
    （不规定 S6 必须有 ATR、不规定 LONG 必须 OPEN、不规定 score 下限）。
    """
    errors: list[str] = []

    if not journal.journal_version or not isinstance(journal.journal_version, str):
        errors.append("journal_version 必须为非空字符串")
    if not journal.journal_id or not isinstance(journal.journal_id, str):
        errors.append("journal_id 必须为非空字符串")
    if not _is_valid_iso(journal.created_at):
        errors.append("created_at 不是合法的 UTC ISO-8601 时间")
    if not _is_valid_iso(journal.decision_at):
        errors.append("decision_at 不是合法的 UTC ISO-8601 时间")

    if journal.signal is None:
        errors.append("signal 必须存在")
    else:
        if not journal.signal.symbol:
            errors.append("signal.symbol 不能为空")
        if journal.signal.side not in SIDES:
            errors.append(f"signal.side 非法: {journal.signal.side!r}（允许 {SIDES}）")

    stages = [g.stage for g in journal.gates]
    if len(stages) != len(set(stages)):
        errors.append("gates.stage 存在重复")
    if stages:
        expected = list(range(1, len(stages) + 1))
        if stages[0] != 1:
            errors.append("gates.stage 必须从 1 开始")
        if stages != expected:
            errors.append(f"gates.stage 必须连续: 期望 {expected}, 实际 {stages}")

    if journal.decision is None:
        errors.append("decision 必须存在")
    else:
        if journal.decision.action not in ACTIONS:
            errors.append(f"decision.action 非法: {journal.decision.action!r}（允许 {ACTIONS}）")

    # 全量 JSON 可序列化检查（NaN/自定义对象会在此暴露）
    try:
        import json as _json

        from journal.serializer import to_dict as _to_dict  # 局部导入避免循环
        _json.dumps(_to_dict(journal), allow_nan=False)
    except JournalValidationError:
        raise
    except (TypeError, ValueError) as exc:
        errors.append(f"Journal 存在不可 JSON 序列化的内容: {exc}")

    return errors
