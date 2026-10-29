from copy import deepcopy
from decimal import Decimal

import pytest
from test_v2_quant_indicators import minutes

from v2_core.quant_indicators import Bar, aggregate_minutes
from v2_core.quant_replay import ReplayPolicy, replay


def scenario(extra=4):
    bars = list(
        aggregate_minutes(
            minutes(breakout=True), timeframe_minutes=5, closed_at_ms=86400000
        )
    )
    for i in range(extra):
        bars.append(Bar(86400000 + i * 300000, "102", "102.1", "101.9", "102", "150"))
    return bars


def test_next_bar_entry_open_position_is_not_fake_closed():
    bars = scenario(0)
    before = deepcopy(bars)
    result = replay(bars, timeframe_minutes=5)
    assert result["pending_signal"] and result["open_position"] is None
    assert not result["trades"] and result["summary"]["cash"] == "1000"
    result = replay(scenario(1), timeframe_minutes=5)
    pos = result["open_position"]
    assert pos["opened_at_ms"] == pos["signal_at_ms"] == 86400000
    assert Decimal(pos["entry"]) > 102  # adverse slippage
    assert Decimal(result["summary"]["fees"]) > 0
    assert result["summary"]["closed_trades"] == 0
    assert bars == before


def test_no_future_data_changes_earlier_decisions():
    bars = scenario(4)
    early = replay(bars[:-2], timeframe_minutes=5)
    bars[-1] = Bar(bars[-1].t, "102", "110", "90", "98", "1000")
    late = replay(bars, timeframe_minutes=5)
    assert early["decisions"] == late["decisions"][: len(early["decisions"])]
    assert early["curve"] == late["curve"][: len(early["curve"])]


def test_ambiguous_bar_is_stop_first_and_costs_reconcile():
    bars = scenario(1)
    bars[-1] = Bar(bars[-1].t, "102", "110", "95", "102", "150")
    result = replay(bars, timeframe_minutes=5)
    trade = result["trades"][0]
    assert trade["reason"] == "STOP_FIRST_AMBIGUOUS_BAR"
    assert Decimal(trade["net_pnl"]) == Decimal(trade["gross_pnl"]) - Decimal(
        trade["fees"]
    ) + Decimal(trade["funding"])
    assert abs(
        Decimal(result["summary"]["cash"]) - 1000 - Decimal(trade["net_pnl"])
    ) < Decimal("1e-15")
    assert Decimal(trade["net_pnl"]) < 0


def test_funding_only_carried_inventory_and_reject_intrabar_ambiguity():
    bars = scenario(3)
    events = [
        {"at_ms": 86400000 + i * 300000, "rate": ".001", "mark_price": "102"}
        for i in range(3)
    ]
    result = replay(bars, timeframe_minutes=5, funding=events)
    qty = Decimal(result["open_position"]["quantity"])
    assert (
        Decimal(result["summary"]["funding"])
        == -qty * Decimal(102) * Decimal(".001") * 2
    )
    with pytest.raises(ValueError, match="FINER_BARS"):
        replay(
            bars,
            timeframe_minutes=5,
            funding=[{**events[0], "at_ms": events[0]["at_ms"] + 1}],
        )
    with pytest.raises(ValueError, match="DUPLICATE"):
        replay(bars, timeframe_minutes=5, funding=[events[0], events[0]])


def test_timeout_next_bar_and_gap_stop_does_not_fill_at_old_stop():
    result = replay(
        scenario(3), timeframe_minutes=5, policy=ReplayPolicy(max_hold_bars=1)
    )
    assert result["trades"][0]["reason"] == "TIME_EXIT"
    assert result["trades"][0]["closed_bar_at_ms"] == 86700000
    bars = scenario(2)
    bars[-1] = Bar(bars[-1].t, "95", "96", "94", "95", "10")
    result = replay(bars, timeframe_minutes=5)
    assert Decimal(result["trades"][0]["exit_price"]) < 95


@pytest.mark.parametrize(
    "changes",
    [
        {"pool_fraction": "1"},
        {"risk_fraction": ".9"},
        {"leverage": 100},
        {"quantity_step": "0"},
        {"fee_rate": ".1"},
        {"max_hold_bars": True},
    ],
)
def test_replay_policy_cannot_spend_all_margin(changes):
    with pytest.raises(ValueError):
        ReplayPolicy(**changes)


def test_replay_warmup_gaps_price_chase_and_determinism():
    bars = scenario(1)
    assert replay(bars, timeframe_minutes=5) == replay(bars, timeframe_minutes=5)
    assert not replay(bars[:10], timeframe_minutes=5)["decisions"]
    with pytest.raises(ValueError, match="GAP"):
        replay(bars[1:5] + bars[6:], timeframe_minutes=5)
    bars[-1] = Bar(bars[-1].t, "120", "121", "119", "120", "100")
    assert replay(bars, timeframe_minutes=5)["open_position"] is None
