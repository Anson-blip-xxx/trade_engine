"""Execution-candidate fixtures without imports from undeployed research code."""

from dataclasses import asdict

from v2_core.directional_exit import ExitFacts, evaluate_directional_exit
from v2_core.runtime_policy import resolve

HOUR = 3_600_000


def snapshot(side="BUY", *, at=24 * HOUR + 300000, opened=None):
    opened = at - 90 * 60000 if opened is None else opened
    facts = ExitFacts(
        system_tag="S8",
        side=side,
        entry_price="100",
        mark_price="100.5" if side == "BUY" else "99.5",
        stop_price="95" if side == "BUY" else "105",
        remaining_quantity="1",
        planned_loss="5",
        held_ms=at - opened,
        funding_rate="0",
        ema9_1h="100" if side == "BUY" else "99",
        ema20_1h="99" if side == "BUY" else "100",
        momentum_closes_15m=("100", "100.1", "100.2", "100.3")
        if side == "BUY"
        else ("100", "99.9", "99.8", "99.7"),
        peak_return_pct="0.5",
        partial_done=False,
        exit_fee_rate="0.0005",
    )
    history = []
    for hour in range(24):
        close = 80 + hour if side == "BUY" else 120 - hour
        history.append(
            {
                "t": hour * HOUR,
                "o": str(close),
                "h": str(close + 1),
                "l": str(close - 1),
                "c": str(close),
                "v": "100",
                "available_at_ms": (hour + 1) * HOUR,
            }
        )
    values = resolve()
    return {
        "at_ms": at,
        "facts": {
            **asdict(facts),
            "momentum_closes_15m": list(facts.momentum_closes_15m),
        },
        "baseline": evaluate_directional_exit(facts, policy=values).evidence(),
        "baseline_policy": values,
        "observation": {
            "observed_at_ms": at,
            "available_at_ms": at,
            "closed_15m_at_ms": at // 900000 * 900000,
            "closed_1h_at_ms": at // HOUR * HOUR,
            "environment": "SANDBOX",
            "market_source": "BINANCE_USDM_SANDBOX",
        },
        "native_protection": {
            "status": "NEW",
            "price": facts.stop_price,
            "protected_quantity": "1",
            "observed_at_ms": at,
            "available_at_ms": at,
        },
        "hourly_history": history,
    }
