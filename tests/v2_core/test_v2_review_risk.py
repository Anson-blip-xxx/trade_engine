"""Factual risk math is not account return, a forecast, or execution policy."""

from copy import deepcopy
from decimal import Decimal

import pytest

from services.v2_review_risk import risk_statistics, trade_risk
from services.v2_trade_review import build_review


def row(**changes):
    return dict(
        id="a",
        symbol="BTCUSDT",
        source="s3",
        side="BUY",
        decided_ms=1,
        opened_ms=2,
        closed_ms=3,
        close_known_ms=4,
        maintenance=False,
        net_pnl="3.5",
        entry_mode="RIGHT_MOMENTUM",
        strategy_version="v1",
        config_digest="config-a",
        opening_quantity="5",
        opening_notional="500",
        sizing={
            "reason": "SIZED",
            "quantity": "5",
            "notional": "500",
            "stop_price": "98",
            "planned_loss": "10",
        },
        **changes,
    )


def changed(**changes):
    result = row()
    result.update(changes)
    return result


def test_r_units_are_not_account_percentage_and_costs_not_deducted_twice():
    trade = changed(fees_usdt="2")
    before = deepcopy(trade)
    result = trade_risk(trade)
    assert result["realized_net_r"] == "0.35"
    assert result["initial_stop_risk_usdt"] == "10"
    assert result["planned_reward_risk"] is None
    assert trade == before


def test_partial_fill_normalizes_planned_risk_and_drift_uses_actual_entry():
    result = trade_risk(
        changed(opening_quantity="2", opening_notional="202", net_pnl="3")
    )
    assert Decimal(result["planned_risk_at_filled_quantity_usdt"]) == 4
    assert Decimal(result["initial_stop_risk_usdt"]) == 6
    assert Decimal(result["entry_risk_drift_pct"]) == 50
    assert Decimal(result["realized_net_r"]) == Decimal("0.5")


@pytest.mark.parametrize("side", ["SELL", "SHORT"])
def test_short_side_and_loss_beyond_risk(side):
    trade = changed(side=side, net_pnl="-12")
    trade["sizing"]["stop_price"] = "102"
    result = trade_risk(trade)
    assert result["realized_net_r"] == "-1.2"
    assert result["net_loss_beyond_initial_risk_usdt"] == "2"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "NaN",
        "Infinity",
        "-Infinity",
        "0",
        "-1",
        True,
        "bad",
        "1e999999",
        "1e-999999",
    ],
)
def test_invalid_evidence_is_unknown_not_zero(value):
    trade = row()
    trade["sizing"]["planned_loss"] = value
    result = trade_risk(trade)
    assert result["status"] != "AVAILABLE"
    assert result["realized_net_r"] is None
    assert result["initial_stop_risk_usdt"] is None


def test_inconsistent_original_plan_and_wrong_side_of_fill_rejected():
    trade = row()
    trade["sizing"]["planned_loss"] = "9"
    assert trade_risk(trade)["status"] == "INCONSISTENT_SIZING_EVIDENCE"
    assert (
        trade_risk(changed(opening_notional="480"))["status"]
        == "STOP_NOT_ON_LOSS_SIDE_OF_FILL"
    )
    assert trade_risk(changed(side="UNKNOWN"))["status"] == "SIDE_UNKNOWN"


def test_small_price_precision():
    trade = changed(
        opening_quantity="1000000", opening_notional="0.000003", net_pnl="0.0000005"
    )
    trade["sizing"] = {
        "reason": "SIZED",
        "quantity": "1000000",
        "notional": "0.000003",
        "stop_price": "0.000000000002",
        "planned_loss": "0.000001",
    }
    assert Decimal(trade_risk(trade)["realized_net_r"]) == Decimal("0.5")


def test_mixed_coverage_breakeven_and_maintenance_denominators():
    trades = [
        changed(net_pnl="20"),
        changed(net_pnl="-10"),
        changed(net_pnl="0"),
        changed(sizing=None, net_pnl="2"),
        changed(net_pnl=None),
        changed(maintenance=True, net_pnl="999"),
    ]
    result = risk_statistics(trades)
    assert result["settled_count"] == 4
    assert result["historical_mean_net_usdt"] == "3"
    assert result["average_net_win_usdt"] == "11"
    assert result["realized_payoff_ratio"] == "1.1"
    assert result["r_sample_count"] == 3 and result["r_missing_count"] == 1
    assert result["r_missing_reasons"] == {"MISSING_SIZING_EVIDENCE": 1}
    assert result["forecast_expectancy"] is result["ruin_probability"] is None


@pytest.mark.parametrize("values", [[], ["0"], ["2"], ["-2"]])
def test_empty_one_sided_samples_do_not_invent_payoff(values):
    result = risk_statistics([changed(net_pnl=v) for v in values])
    assert result["realized_payoff_ratio"] is None
    assert result["settled_count"] == len(values)
    if not values:
        assert (
            result["historical_mean_net_usdt"]
            is result["historical_mean_net_r"]
            is None
        )


def test_report_does_not_leak_future_pnl_or_mix_versions():
    records = [
        row(),
        changed(id="b", strategy_version="v2"),
        changed(id="c", config_digest="config-b"),
        changed(id="d", closed_ms=None, net_pnl="999"),
    ]
    original = deepcopy(records)
    report = build_review(records, start_ms=0, now_ms=10)
    assert len(report["groups"]) == 3
    assert report["risk_summary"]["settled_count"] == 3
    assert report["trades"][-1]["risk"]["realized_net_r"] is None
    assert (
        not report["automatic_parameter_updates"] and not report["execution_authorized"]
    )
    assert records == original


def test_outcomes_do_not_change_denominator():
    a, b = trade_risk(changed(net_pnl="20")), trade_risk(changed(net_pnl="-20"))
    for field in (
        "initial_stop_risk_usdt",
        "planned_risk_at_filled_quantity_usdt",
        "entry_risk_drift_pct",
    ):
        assert a[field] == b[field]


def test_recurring_decimal_r_survives_summary_without_float_rounding():
    trade = changed(opening_quantity="1.5", opening_notional="150", net_pnl="1")
    result = risk_statistics([trade])
    assert result["r_sample_count"] == 1
    assert result["historical_mean_net_r"].startswith(
        "0.333333333333333333333333333333"
    )


def test_extreme_valid_r_ratio_stays_representable():
    trade = changed(
        opening_quantity="1",
        opening_notional="0.000000000000000002",
        net_pnl="10000000000000000000",
    )
    trade["sizing"] = {
        "reason": "SIZED",
        "quantity": "1",
        "notional": "0.000000000000000002",
        "stop_price": "0.000000000000000001",
        "planned_loss": "0.000000000000000001",
    }
    result = risk_statistics([trade])
    assert Decimal(result["historical_mean_net_r"]) == Decimal("1e37")
