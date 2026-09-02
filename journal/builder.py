"""DecisionJournalBuilder — 把决策链上下文组装成 DecisionJournal（P1-02）。

职责边界：
- 只"组装"：接收调用方已经算好的结果（gate 判定、score、risk 输出、行情快照值）
- 不计算指标、不判断趋势、不发 IO（无 Redis/DB/网络/磁盘）
- **所有方法 fail-safe**：任何异常在内部吞掉，绝不向交易链路抛出

用法（S6/S8 旁路接入模式）::

    jb = DecisionJournalBuilder(signal_source='S3', signal_type=evt['type'], ...)
    jb.gate('regime', regime_ok, value=regime)      # 只记录实际执行过的 Gate
    if not regime_ok:
        safe_record(get_default_recorder(), jb, action='REJECT',
                    accepted=False, reason='regime_conflict')
        return state                                # 原有 return 语义不变
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

from journal.models import (
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    RiskSnapshot,
    SignalSnapshot,
    StrategySnapshot,
    iso_utc,
    utc_now_iso,
)

__all__ = ["DecisionJournalBuilder", "signal_source_from_event"]


def _known_fields(cls) -> set:
    return {f.name for f in dataclasses.fields(cls)}


_MARKET_FIELDS = _known_fields(MarketSnapshot)
_REGIME_FIELDS = _known_fields(RegimeSnapshot)
_STRATEGY_FIELDS = _known_fields(StrategySnapshot)
_METADATA_FIELDS = _known_fields(Metadata)


def _epoch_to_iso(ts: Any) -> Any:
    """epoch 秒 → UTC ISO-8601；其他类型原样返回。"""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return iso_utc(datetime.fromtimestamp(ts, tz=timezone.utc))
    return ts


def signal_source_from_event(evt: dict) -> str:
    """从事件 payload 推断来源标签。

    - 无 source 标记 → 'S3'（S6/S8 的事件快照默认来自 S3）
    - 'tv' / 'TRADINGVIEW' → 'TRADINGVIEW'
    - 其他值大写透传（source-agnostic）
    """
    src = (evt or {}).get('source')
    if not src:
        return 'S3'
    src = str(src).upper()
    if src in ('TV', 'TRADINGVIEW'):
        return 'TRADINGVIEW'
    return src


class DecisionJournalBuilder:
    """增量组装 DecisionJournal；所有方法永不抛异常（fail-safe）。"""

    def __init__(self, *, signal_source: str = 'UNKNOWN', signal_type: str = '',
                 symbol: str = '', side: str = 'UNKNOWN', strength: Any = None,
                 signal_timestamp: Any = None, event_id: Any = None, raw: dict | None = None,
                 strategy: str | None = None, strategy_version: str | None = None,
                 entry_mode: str | None = None, score: Any = None,
                 environment: str | None = None, process: str | None = None,
                 pid: int | None = None, git_commit: str | None = None,
                 config_version: str | None = None, correlation_id: str | None = None,
                 parent_signal_id: str | None = None):
        try:
            if strength is not None:
                try:
                    strength = float(strength)
                except (TypeError, ValueError):
                    strength = None  # 解析不了就记 None，不猜
            self._signal_kwargs = {
                'source': signal_source, 'signal_type': signal_type, 'symbol': symbol,
                'side': side, 'strength': strength,
                'signal_timestamp': _epoch_to_iso(signal_timestamp),
                'event_id': event_id, 'raw': raw,
            }
            self._strategy_kwargs = {'strategy': strategy, 'version': strategy_version,
                                     'score': score, 'entry_mode': entry_mode}
            self._metadata_kwargs = {'environment': environment, 'process': process,
                                     'pid': pid, 'git_commit': git_commit,
                                     'config_version': config_version,
                                     'correlation_id': correlation_id,
                                     'parent_signal_id': parent_signal_id}
            self._market_kwargs: dict = {}
            self._regime_kwargs: dict = {}
            self._gates: list = []
            self._risk: RiskSnapshot | None = None
            self._broken = False
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True

    # ── Gate 记录 ────────────────────────────────────────────────────────
    def gate(self, name: str, passed: Any, *, value: Any = None, threshold: Any = None,
             reason: str | None = None, timestamp: Any = None,
             duration_ms: float | None = None, metadata: dict | None = None) -> bool:
        """记录一个**实际执行过**的 Gate；返回 passed（便于调用方复用判定值）。

        stage 自动按记录顺序递增（1..N）。不要用本方法重跑 Gate。
        """
        passed = bool(passed)
        try:
            if not self._broken:
                self._gates.append(GateResult(
                    name=name, stage=len(self._gates) + 1, passed=passed, value=value,
                    threshold=threshold, reason=reason, timestamp=timestamp,
                    duration_ms=duration_ms, metadata=metadata))
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True  # Journal 出错：停止记录，绝不影响交易
        return passed

    # ── 快照字段注入（只接收已存在的值） ─────────────────────────────────
    def set_market(self, **fields) -> None:
        try:
            if not self._broken:
                self._market_kwargs.update(
                    {k: v for k, v in fields.items() if k in _MARKET_FIELDS})
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True

    def set_regime(self, *, regime: Any = None, risk_level: Any = None,
                   confidence: Any = None, source: Any = None,
                   timestamp: Any = None) -> None:
        try:
            if self._broken:
                return
            self._regime_kwargs.update({'regime': regime, 'risk_level': risk_level,
                                        'confidence': confidence, 'source': source,
                                        'timestamp': _epoch_to_iso(timestamp)})
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True

    def set_strategy_score(self, score: Any) -> None:
        try:
            if not self._broken:
                self._strategy_kwargs['score'] = (
                    float(score) if score is not None else None)
        except (TypeError, ValueError):
            self._strategy_kwargs['score'] = None
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True

    def set_entry_mode(self, mode: Any) -> None:
        try:
            if not self._broken:
                self._strategy_kwargs['entry_mode'] = (
                    None if mode is None else str(mode))
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True

    def set_risk(self, *, risk_level: Any = None, position_size: Any = None,
                 position_fraction: Any = None, leverage: Any = None,
                 max_loss: Any = None, stop_pct: Any = None,
                 take_profit_pct: Any = None, timestamp: Any = None,
                 metadata: dict | None = None) -> None:
        """记录现有 Risk 层输出；Risk 构造失败只丢弃 risk，不拖累整个 Journal。"""
        try:
            if not self._broken:
                self._risk = RiskSnapshot(
                    risk_level=risk_level, position_size=position_size,
                    position_fraction=position_fraction, leverage=leverage,
                    max_loss=max_loss, stop_pct=stop_pct,
                    take_profit_pct=take_profit_pct, timestamp=timestamp,
                    metadata=metadata or {})
        except Exception:  # noqa: BLE001 — Risk 记录失败不拖累 Journal
            self._risk = None

    # ── 组装 ─────────────────────────────────────────────────────────────
    def build(self, *, action: str = 'NO_ACTION', accepted: bool = False,
              reason: str | None = None, final_score: Any = None,
              decision_at: Any = None) -> DecisionJournal | None:
        """组装最终 Journal。失败返回 None（不抛异常，交易链路无感）。"""
        try:
            if self._broken:
                return None
            return DecisionJournal(
                signal=SignalSnapshot(**self._signal_kwargs),
                market=MarketSnapshot(**self._market_kwargs),
                regime=RegimeSnapshot(**self._regime_kwargs),
                strategy=StrategySnapshot(**self._strategy_kwargs),
                gates=tuple(self._gates),
                decision=DecisionResult(action=action, accepted=bool(accepted),
                                        reason=reason, final_score=final_score,
                                        timestamp=_epoch_to_iso(decision_at) or utc_now_iso()),
                risk=self._risk,
                metadata=Metadata(**self._metadata_kwargs),
            )
        except Exception:  # noqa: BLE001 — fail-open 是本模块的设计要求
            self._broken = True
            return None
