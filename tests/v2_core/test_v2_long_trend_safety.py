"""Independent multi-cycle accounting and causal replay safety checks."""

from copy import deepcopy
from decimal import Decimal
from random import Random

import pytest

from v2_core.long_trend import DAY, HOUR, LongTrendPolicy, replay
from v2_core.quant_indicators import Bar

D = Decimal
EPS = D("1e-14")


def multi_cycle(seed):
    random = Random(seed)
    bars = []
    previous = D(100)
    for i in range(75 * 24):
        # Repeated trending, noisy, and gapping regimes: not a tuned fixture.
        day = i // 24
        change = D("0.12") if day % 15 < 10 else D("-0.4")
        close = max(D(20), previous + change + D(random.randint(-4, 4)) / 100)
        opening = previous if i % (15 * 24) else max(D(20), previous - 2)
        bars.append(
            Bar(
                i * HOUR,
                opening,
                max(opening, close) + D(".15"),
                min(opening, close) - D(".15"),
                close,
                "1000",
            )
        )
        previous = close
    events = [
        {
            "at_ms": i * 4 * HOUR + 17,
            "rate": ".0001" if i % 3 else "-.00005",
            "mark_price": str(bars[i * 4].c),
        }
        for i in range(len(bars) // 4)
    ]
    return bars, events


def policy():
    return LongTrendPolicy(breakout_days=3, atr_days=3, structure_days=3)


@pytest.mark.parametrize("seed", [1, 9, 37])
def test_multi_cycle_ledger_and_capital_invariants(seed):
    bars, events = multi_cycle(seed)
    before = deepcopy(events)
    report = replay(bars, policy=policy(), funding=events, closed_at_ms=75 * DAY)
    assert events == before
    assert len(report["trades"]) >= 3
    closed_pnl = sum((D(t["net_pnl"]) for t in report["trades"]), D(0))
    position = report["open_position"]
    open_fees = D(position["fees"]) if position else D(0)
    open_funding = D(position["funding"]) if position else D(0)
    summary = report["summary"]
    assert (
        abs(D(summary["cash"]) - (1000 + closed_pnl - open_fees + open_funding)) < EPS
    )
    assert abs(D(summary["realized_net_pnl"]) - closed_pnl) < EPS
    fees = sum((D(t["fees"]) for t in report["trades"]), open_fees)
    funding = sum((D(t["funding"]) for t in report["trades"]), open_funding)
    assert abs(D(summary["fees"]) - fees) < EPS
    assert abs(D(summary["funding"]) - funding) < EPS
    for trade in report["trades"]:
        gross = D(trade["quantity"]) * (
            D(trade["exit_price"]) - D(trade["entry_price"])
        )
        assert abs(D(trade["gross_pnl"]) - gross) < EPS
        assert (
            abs(D(trade["net_pnl"]) - gross + D(trade["fees"]) - D(trade["funding"]))
            < EPS
        )
        assert (
            trade["signal_at_ms"] <= trade["opened_at_ms"] <= trade["closed_bar_at_ms"]
        )
    for entry in report["entries"]:
        cash = D(entry["entry_cash"])
        assert D(entry["initial_margin"]) + D(entry["fees"]) <= cash * D(".10")
        assert D(entry["initial_stop_risk_including_costs"]) <= cash * D(".005")
        assert D(entry["quantity"]) % D(policy().quantity_step) == 0
    high, mdd = D(1000), D(0)
    for point in report["curve"]:
        equity = D(point["equity"])
        assert abs(equity - D(point["cash"]) - D(point["unrealized"])) < EPS
        high = max(high, equity)
        mdd = max(mdd, (high - equity) / high)
    assert abs(D(summary["max_drawdown_fraction"]) - mdd) < EPS


def test_future_funding_and_price_shock_cannot_change_prior_executions():
    bars, events = multi_cycle(9)
    split = 45 * 24
    boundary = split * HOUR
    early = replay(
        bars[:split],
        policy=policy(),
        funding=[e for e in events if e["at_ms"] < boundary],
        closed_at_ms=boundary,
    )
    altered = bars[:split] + [
        Bar(b.t, "5", "6", "4", "5", "1000") for b in bars[split:]
    ]
    events.append({"at_ms": boundary + 1, "rate": "1", "mark_price": "1000000"})
    late = replay(altered, policy=policy(), funding=events, closed_at_ms=75 * DAY)
    for key in ("decisions", "entries", "entry_attempts", "trades", "curve"):
        assert early[key] == late[key][: len(early[key])]


def test_uncounted_funding_is_explicit_even_when_history_is_empty():
    bars, _ = multi_cycle(1)
    result = replay(bars, policy=policy(), funding=[], funding_complete=False)
    assert result["summary"]["funding"] == "0"
    assert result["funding_complete"] is False
    assert result["cost_complete"] is False
    assert result["profitability_valid"] is False
    assert result["execution_authorized"] is False
    assert "UNVERIFIED" in result["assumptions"]["funding_completeness"]


def test_initial_stop_risk_budget_is_not_misrepresented_as_guaranteed_gap_loss():
    bars = [Bar(i * HOUR, "100", "101", "99", "100", "1000") for i in range(4 * 24)]
    bars[-1] = Bar(bars[-1].t, "100", "105", "99", "104", "1000")
    bars += [
        Bar(4 * DAY, "104", "105", "103", "104", "1000"),
        Bar(4 * DAY + HOUR, "30", "31", "29", "30", "1000"),
    ]
    p = LongTrendPolicy(breakout_days=3, atr_days=3, structure_days=0)
    result = replay(bars, policy=p)
    trade = result["trades"][0]
    risk = D(result["entries"][0]["initial_stop_risk_including_costs"])
    assert trade["reason"] == "GAP_STOP"
    assert -D(trade["net_pnl"]) > risk
    assert D(trade["exit_price"]) < 30
    assert result["profitability_valid"] is False
