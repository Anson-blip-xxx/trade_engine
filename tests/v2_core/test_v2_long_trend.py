"""Causal, adverse-execution and capital-accounting contracts for paper trends."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from v2_core.long_trend import DAY, HOUR, LongTrendPolicy, replay
from v2_core.quant_indicators import Bar


def hour(t, close="100", *, open_=None, high=None, low=None):
    c = Decimal(close)
    o = c if open_ is None else Decimal(open_)
    return Bar(
        t,
        o,
        max(o, c) + 1 if high is None else high,
        min(o, c) - 1 if low is None else low,
        c,
        "1000",
    )


def scenario(extra=1):
    # Days 0-2 are flat; the fourth completed day closes above all prior highs.
    bars = [hour(i * HOUR) for i in range(4 * 24)]
    bars[-1] = hour(bars[-1].t, "104", open_="100", high="105", low="99")
    bars += [hour(4 * DAY + i * HOUR, "104") for i in range(extra)]
    return bars


def policy(**kwargs):
    return LongTrendPolicy(breakout_days=3, atr_days=3, structure_days=0, **kwargs)


def result(extra=1, **kwargs):
    return replay(scenario(extra), policy=policy(), **kwargs)


def dec(value):
    return Decimal(value)


def test_defaults_are_frozen_research_only_and_have_30_percent_reserve():
    p = LongTrendPolicy()
    assert p.breakout_days == 20 and p.leverage == 1
    assert dec(p.pool_fraction) == Decimal(".70")
    with pytest.raises(FrozenInstanceError):
        p.leverage = 3
    report = result()
    assert report["execution_authorized"] is False
    assert report["profitability_valid"] is False
    assert report["funding_complete"] is False
    assert report["cost_complete"] is False
    assert (
        replay(scenario(), policy=policy(), funding_complete=True)[
            "profitability_valid"
        ]
        is False
    )
    json.dumps(report, allow_nan=False)


def test_completed_daily_only_and_next_hour_fill_not_signal_close():
    pending = result(0)
    assert pending["open_position"] is None and pending["pending_signal"]
    assert pending["pending_signal"]["signal_at_ms"] == 4 * DAY
    assert pending["decisions"][-1]["status"] == "ENTRY_SIGNAL_NEXT_OPEN"
    report = result()
    pos = report["open_position"]
    assert pos["opened_at_ms"] == 4 * DAY
    assert pos["signal_at_ms"] == 4 * DAY
    assert dec(pos["entry"]) > 104  # Next open plus adverse slippage.
    assert not report["trades"] and not report["pending_signal"]
    assert report["summary"]["closed_trades"] == 0


def test_partial_leading_and_trailing_days_are_not_signal_days():
    bars = scenario()
    before = deepcopy(bars)
    report = replay(bars[5:], policy=policy())
    assert report["summary"]["completed_days"] == 3
    assert all(d["status"] == "WARMUP" for d in report["decisions"])
    assert not report["entries"]
    assert bars == before


def test_no_future_data_changes_prefix_decisions_curve_or_entries():
    bars = scenario(2 * 24)
    early = replay(bars[:-24], policy=policy())
    bars[-1] = hour(bars[-1].t, "110", open_="104", high="1000", low="100")
    late = replay(bars, policy=policy())
    for field in ("decisions", "curve", "entries", "entry_attempts"):
        assert early[field] == late[field][: len(early[field])]
    assert result() == result()


def test_daily_atr_stop_ratchet_applies_only_to_next_hour():
    bars = scenario(24)
    # Last hour's low breaches the future ratchet, but not the active stop.
    bars[-1] = hour(bars[-1].t, "119", open_="104", high="120", low="101")
    p = policy(atr_multiple="1")
    first = replay(bars, policy=p)
    assert not first["trades"]
    decision = first["decisions"][-1]
    assert dec(decision["next_bar_stop"]) > 101 > dec(decision["prior_stop"])
    bars.append(hour(5 * DAY, "106", high="107", low="105"))
    after = replay(bars, policy=p)
    assert after["trades"][0]["reason"] == "GAP_STOP"
    assert dec(after["trades"][0]["exit_price"]) < 106


def test_daily_stop_never_decreases_when_volatility_expands():
    bars = scenario(24)
    bars[-1] = hour(bars[-1].t, "110", open_="104", high="111", low="103")
    bars += [hour(5 * DAY + i * HOUR, "111", high="140", low="103") for i in range(24)]
    report = replay(bars, policy=policy())
    holds = [d for d in report["decisions"] if d["status"] == "HOLD_TREND"]
    assert holds
    assert all(dec(d["next_bar_stop"]) >= dec(d["prior_stop"]) for d in holds)


def test_structure_stop_is_optional_and_configurable():
    plain = replay(scenario(), policy=policy())["open_position"]
    structured = replay(scenario(), policy=replace(policy(), structure_days=3))[
        "open_position"
    ]
    assert dec(plain["initial_stop"]) == 94
    assert dec(structured["initial_stop"]) == 99


def test_daily_atr_uses_wilder_seed_and_only_closed_days():
    report = result(24)
    decisions = report["decisions"]
    assert decisions[0]["atr"] is None and decisions[1]["atr"] is None
    assert dec(decisions[2]["atr"]) == 2
    assert abs(dec(decisions[3]["atr"]) - Decimal(10) / 3) < Decimal("1e-18")
    assert abs(dec(decisions[4]["atr"]) - Decimal(26) / 9) < Decimal("1e-18")
    assert len(result(23)["decisions"]) == 4


def test_as_of_and_cost_assumptions_distinguish_caller_assertions_from_observation():
    bars = scenario()
    inferred = replay(bars, policy=policy())
    explicit = replay(bars, policy=policy(), closed_at_ms=bars[-1].t + HOUR)
    assert inferred["assumptions"]["as_of"] == "CALLER_ASSERTED_CLOSED_BARS"
    assert explicit["assumptions"]["as_of"] == "CALLER_SUPPLIED_CLOSED_BOUNDARY"
    assert inferred["costs_observed"] is False
    assert (
        inferred["assumptions"]["cost_completeness_scope"]
        == "CONFIGURED_FEES_SLIPPAGE_AND_CALLER_ASSERTED_FUNDING_ONLY"
    )


def test_no_fixed_time_exit_no_fixed_profit_target_or_pyramiding():
    bars = scenario(60 * 24)
    report = replay(bars, policy=policy())
    assert not report["trades"]
    assert len(report["entries"]) == 1
    assert report["open_position"]["opened_at_ms"] == 4 * DAY


def test_stop_first_same_bar_high_cannot_rescue_loss_and_costs_reconcile():
    bars = scenario()
    bars[-1] = hour(bars[-1].t, "104", high="200", low="80")
    report = replay(bars, policy=policy())
    trade = report["trades"][0]
    assert trade["reason"] == "PROTECTIVE_STOP"
    assert dec(trade["exit_price"]) < dec(trade["initial_stop"])
    assert dec(trade["net_pnl"]) < 0
    assert abs(
        dec(trade["net_pnl"])
        - (dec(trade["gross_pnl"]) - dec(trade["fees"]) + dec(trade["funding"]))
    ) < Decimal("1e-15")
    assert abs(dec(report["summary"]["cash"]) - 1000 - dec(trade["net_pnl"])) < Decimal(
        "1e-15"
    )


def test_gap_stop_fills_at_adverse_open_not_stale_stop():
    bars = scenario(2)
    bars[-1] = hour(bars[-1].t, "80", high="82", low="79")
    trade = replay(bars, policy=policy())["trades"][0]
    assert trade["reason"] == "GAP_STOP"
    assert dec(trade["exit_price"]) < 80 < dec(trade["exit_stop"])


def test_entry_sizing_respects_risk_margin_fees_reserve_and_venue_rounding():
    for p in (policy(), policy(risk_fraction=".70", max_margin_fraction=".70")):
        report = replay(scenario(), policy=p)
        entry = report["entries"][0]
        assert dec(entry["initial_stop_risk_including_costs"]) <= 1000 * dec(
            p.risk_fraction
        )
        committed = dec(entry["initial_margin"]) + dec(entry["fees"])
        assert committed <= 1000 * dec(p.max_margin_fraction)
        assert committed <= 700
        assert dec(entry["quantity"]) % dec(p.quantity_step) == 0
        assert dec(entry["entry"]) % dec(p.price_tick) == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"minimum_quantity": "100"}, "QUANTITY_BELOW_MINIMUM"),
        ({"minimum_notional": "1000"}, "NOTIONAL_BELOW_MINIMUM"),
    ],
)
def test_unfillable_quantity_does_not_create_paper_trade(change, reason):
    report = replay(scenario(), policy=replace(policy(), **change))
    assert report["open_position"] is None
    assert report["summary"]["entry_rejections"] == {reason: 1}
    assert not report["trades"] and report["summary"]["cash"] == "1000"


@pytest.mark.parametrize(
    "opening,reason",
    [("80", "STOP_INVALID_AT_NEXT_OPEN"), ("120", "NEXT_OPEN_CHASE_LIMIT")],
)
def test_adverse_next_open_does_not_receive_impossible_old_entry(opening, reason):
    bars = scenario()
    bars[-1] = hour(bars[-1].t, opening)
    report = replay(bars, policy=policy())
    assert not report["entries"]
    assert report["entry_attempts"][0]["reason"] == reason


def test_entry_start_is_decision_time_not_truncated_history():
    allowed = replay(scenario(), policy=policy(), entry_start_ms=4 * DAY)
    blocked = replay(scenario(), policy=policy(), entry_start_ms=4 * DAY + 1)
    assert allowed["entries"]
    assert not blocked["entries"]
    assert blocked["decisions"][-1]["status"] == "BEFORE_ENTRY_WINDOW"


def test_funding_at_boundary_only_charges_carried_inventory():
    funding = [
        {"at_ms": 4 * DAY + i * HOUR, "rate": ".001", "mark_price": "104"}
        for i in range(3)
    ]
    report = result(3, funding=funding, funding_complete=True)
    qty = dec(report["open_position"]["quantity"])
    assert dec(report["summary"]["funding"]) == -qty * 104 * Decimal(".001") * 2
    assert report["funding_complete"] and report["cost_complete"]
    assert report["profitability_valid"] is False


def test_real_funding_timestamp_jitter_is_not_dropped():
    funding = [{"at_ms": 4 * DAY + 1, "rate": ".001", "mark_price": "104"}]
    report = result(funding=funding)
    qty = dec(report["open_position"]["quantity"])
    assert dec(report["summary"]["funding"]) == -qty * 104 * Decimal(".001")


def test_ambiguous_intrahour_funding_charges_debits_but_not_uncertain_credits():
    bars = scenario(2)
    bars[-1] = hour(bars[-1].t, "104", high="106", low="90")
    funding = [
        {"at_ms": bars[-1].t + 1, "rate": ".001", "mark_price": "104"},
        {"at_ms": bars[-1].t + 2, "rate": "-.001", "mark_price": "104"},
    ]
    report = replay(bars, policy=policy(), funding=funding)
    assert dec(report["summary"]["funding"]) < 0
    assert report["summary"]["ambiguous_funding_credits_omitted"] == 1
    trade = report["trades"][0]
    assert abs(
        dec(trade["net_pnl"])
        - (dec(trade["gross_pnl"]) - dec(trade["fees"]) + dec(trade["funding"]))
    ) < Decimal("1e-15")


def test_open_gap_stop_precedes_later_funding():
    bars = scenario(2)
    bars[-1] = hour(bars[-1].t, "80")
    funding = [{"at_ms": bars[-1].t + 1, "rate": ".001", "mark_price": "80"}]
    report = replay(bars, policy=policy(), funding=funding)
    assert report["summary"]["funding"] == "0"


def test_open_inventory_mark_is_not_realized_profit_and_accounting_reconciles():
    bars = scenario(2)
    bars[-1] = hour(bars[-1].t, "120", low="103")
    funding = [{"at_ms": bars[-1].t, "rate": "-.001", "mark_price": "104"}]
    report = replay(bars, policy=policy(), funding=funding)
    summary, curve = report["summary"], report["curve"][-1]
    assert summary["realized_net_pnl"] == "0"
    assert dec(summary["cash"]) == 1000 - dec(summary["fees"]) + dec(summary["funding"])
    assert abs(
        dec(summary["equity"]) - dec(summary["cash"]) - dec(curve["unrealized"])
    ) < Decimal("1e-15")


def test_funding_insolvency_is_flagged_and_never_claims_profitable_validity():
    # Deliberately extreme input stress; not a venue liquidation model.
    events = [{"at_ms": 4 * DAY + HOUR, "rate": "1", "mark_price": "100000"}]
    report = result(2, funding=events, funding_complete=True)
    assert report["summary"]["insolvent_at_ms"] == 4 * DAY + 2 * HOUR
    assert dec(report["summary"]["equity"]) < 0
    assert report["profitability_valid"] is False


def test_insolvent_research_account_cannot_reenter_a_later_breakout():
    bars = scenario(2 * 24 + 1)
    bars[4 * 24 + 2] = hour(4 * DAY + 2 * HOUR, "104", low="90")
    bars[-2] = hour(bars[-2].t, "130", open_="104", high="131", low="103")
    events = [{"at_ms": 4 * DAY + HOUR, "rate": "1", "mark_price": "100000"}]
    report = replay(bars, policy=policy(), funding=events)
    assert len(report["entries"]) == 1
    assert report["decisions"][-1]["status"] == "INSOLVENT_PAPER_ACCOUNT"
    assert report["open_position"] is None and report["pending_signal"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"pool_fraction": ".71"},
        {"pool_fraction": "1"},
        {"risk_fraction": "0"},
        {"risk_fraction": ".11"},
        {"leverage": 2},
        {"leverage": True},
        {"breakout_days": True},
        {"breakout_days": 1},
        {"atr_days": 0},
        {"structure_days": -1},
        {"quantity_step": "0"},
        {"price_tick": "0"},
        {"initial_equity": "NaN"},
        {"fee_rate": "Infinity"},
        {"fee_rate": ".1"},
        {"slippage_fraction": ".2"},
        {"atr_multiple": "0"},
        {"atr_multiple": "21"},
        {"risk_fraction": 0.005},
        {"initial_equity": 1000},
    ],
)
def test_invalid_policy_cannot_remove_reserve_or_use_nonfinite_values(changes):
    with pytest.raises(ValueError):
        LongTrendPolicy(**changes)


@pytest.mark.parametrize(
    "kind",
    ["gap", "duplicate", "reverse", "unaligned", "future", "stale", "empty", "dict"],
)
def test_history_validation(kind):
    bars = scenario()
    kwargs = {}
    if kind == "gap":
        bars.pop(5)
    elif kind == "duplicate":
        bars.insert(5, bars[5])
    elif kind == "reverse":
        bars.reverse()
    elif kind == "unaligned":
        bars = [hour(1)]
    elif kind == "future":
        kwargs["closed_at_ms"] = bars[-1].t
    elif kind == "stale":
        kwargs["closed_at_ms"] = bars[-1].t + 2 * HOUR
    elif kind == "empty":
        bars = []
    else:
        bars = [bars[0].payload()]
    with pytest.raises(ValueError):
        replay(bars, policy=policy(), **kwargs)


@pytest.mark.parametrize(
    "event",
    [
        {"at_ms": -1, "rate": "0", "mark_price": "100"},
        {"at_ms": 0, "rate": "NaN", "mark_price": "100"},
        {"at_ms": 0, "rate": "2", "mark_price": "100"},
        {"at_ms": 0, "rate": "0", "mark_price": "0"},
        {"at_ms": 0, "rate": "0"},
        {"at_ms": True, "rate": "0", "mark_price": "100"},
        {"at_ms": 100 * DAY, "rate": "0", "mark_price": "100"},
    ],
)
def test_funding_validation(event):
    with pytest.raises(ValueError):
        result(funding=[event])


def test_duplicate_funding_and_nonboolean_completeness_rejected():
    event = {"at_ms": 0, "rate": "0", "mark_price": "100"}
    with pytest.raises(ValueError, match="DUPLICATE"):
        result(funding=[event, event])
    with pytest.raises(ValueError):
        result(funding_complete="true")


def test_nonfinite_bar_prices_rejected_before_replay():
    with pytest.raises(ValueError):
        Bar(0, "100", "101", "99", "NaN", "1000")
