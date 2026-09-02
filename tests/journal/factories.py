"""Journal 测试共用工厂：完整 / 最小 Journal 构造。"""
import uuid

from journal import (
    DecisionAction,
    DecisionJournal,
    DecisionResult,
    GateResult,
    MarketSnapshot,
    Metadata,
    RegimeSnapshot,
    Side,
    SignalSnapshot,
    StrategySnapshot,
)

T = "2026-09-01T12:00:00.123Z"


def build_minimal_journal() -> DecisionJournal:
    """最小合法 Journal：只含必须字段。"""
    return DecisionJournal(
        journal_id=str(uuid.uuid4()),
        signal=SignalSnapshot(symbol="BTCUSDT", side=Side.LONG),
        decision=DecisionResult(action=DecisionAction.REJECT, accepted=False, reason="test"),
    )


def build_full_journal() -> DecisionJournal:
    """字段齐全的 Journal（与 docs/v2/DECISION_JOURNAL_SCHEMA.md 示例保持一致）。"""
    return DecisionJournal(
        journal_version="1.0",
        journal_id="0f0e1d2c-3b4a-4958-8677-112233445566",
        created_at=T,
        decision_at=T,
        signal=SignalSnapshot(
            source="S3",
            signal_type="PULSE_UP",
            symbol="BTCUSDT",
            side=Side.LONG,
            strength=78,
            signal_timestamp=T,
            event_id="evt-001",
            raw={"type": "PULSE_UP", "chg_15m": 3.2},
        ),
        market=MarketSnapshot(
            symbol="BTCUSDT",
            price=112300.5,
            bid=112300.4,
            ask=112300.6,
            spread=0.2,
            volume=12345.6,
            volume_ratio=1.8,
            atr=1200.0,
            atr_pct=0.0107,
            ema20=111900.0,
            ema50=111500.0,
            rsi=64.2,
            taker_buy_ratio=0.57,
            open_interest=None,
            funding_rate=None,
            timestamp=T,
            extra={},
        ),
        regime=RegimeSnapshot(
            regime="weak_bull",
            risk_level="NORMAL",
            confidence=0.82,
            source="S0",
            timestamp=T,
            extra={},
        ),
        strategy=StrategySnapshot(
            strategy="S6",
            version="1",
            score=82,
            entry_mode="RIGHT_MOMENTUM",
            timestamp=T,
            extra={},
        ),
        gates=(
            GateResult(name="fresh", stage=1, passed=True, value=True, timestamp=T),
            GateResult(name="market_allowed", stage=2, passed=True,
                       value="weak_bull", threshold="not risk-off", timestamp=T),
            GateResult(name="trend", stage=3, passed=True, value="UP", threshold="UP", timestamp=T),
            GateResult(name="atr", stage=4, passed=False, value=7.2, threshold=6.0,
                       reason="ATR exceeds maximum", timestamp=T),
        ),
        decision=DecisionResult(
            action=DecisionAction.REJECT,
            accepted=False,
            reason="atr_gate_failed",
            final_score=82,
            timestamp=T,
        ),
        risk=None,
        metadata=Metadata(
            environment="REPLAY",
            hostname=None,
            process="S6",
            pid=None,
            git_commit=None,
            config_version=None,
            correlation_id="corr-001",
            parent_signal_id="evt-001",
            extra={},
        ),
    )
