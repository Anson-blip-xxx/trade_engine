"""Exact price attribution without pretending all exits are native stop exits."""

import json
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from v2_core.execution_cost_evidence import build_execution_cost_case


def fixture(side="BUY"):
    trade = {
        "trade_ref": "a" * 64,
        "symbol": "AIOTUSDT",
        "side": side,
        "gross_usdt": "-6.0396",
        "net_usdt": "-6.161111",
        "recorded_fees_usdt": "0.121511",
    }
    accounting = {
        "opening_quantity": "719",
        "closing_quantity": "719",
        "opening_notional": "124.5308",
        "closing_notional": "118.4912",
    }
    plan = {
        "evidence_present": True,
        "planned_entry_price": "0.1729",
        "initial_stop_price": "0.165",
        "intent_quantity": "719",
        "sizing_quantity": "719",
        "result_quantity": "719",
        "sizing_reason": "SIZED",
        "result_action": "OPEN",
        "result_side": side,
        "cost_buffer_fraction": "0.002",
        "budgeted_stop_risk_usdt": "6.5",
        "close_order_count": 1,
        "native_close_count": 1,
        "native_trigger_price": "0.165",
        "native_stop_type_verified": True,
    }
    if side == "SELL":
        accounting.update(opening_notional="118.4912", closing_notional="124.5308")
        plan.update(
            planned_entry_price="0.1651",
            initial_stop_price="0.173",
            native_trigger_price="0.173",
        )
    return trade, accounting, plan


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_stop_decomposition_excludes_fees_and_keeps_existing_buffer(side):
    trade, accounting, plan = fixture(side)
    before = deepcopy((trade, accounting, plan))
    case = build_execution_cost_case(trade, accounting, plan)
    assert (trade, accounting, plan) == before
    assert case["status"] == "AVAILABLE"
    assert case["plan"]["gross_stop_loss_requested_quantity_usdt"] == "5.6801"
    assert case["plan"]["recorded_budgeted_stop_risk_usdt"] == "6.5"
    assert case["plan"]["frozen_cost_buffer_fraction"] == "0.002"
    decomposition = case["stop_decomposition"]
    assert decomposition["status"] == "AVAILABLE"
    assert decomposition["identity_verified"] is True
    assert decomposition["planned_gross_loss_usdt"] == "5.6801"
    assert decomposition["actual_gross_loss_usdt"] == "6.0396"
    assert decomposition["entry_adverse_deviation_usdt"] == "0.2157"
    assert decomposition["stop_fill_adverse_deviation_usdt"] == "0.1438"
    assert decomposition["actual_gross_loss_minus_planned_usdt"] == "0.3595"
    assert case["actual"]["recorded_fees_usdt"] == "0.121511"
    assert case["actual"]["settled_net_usdt"] == "-6.161111"
    assert case["actual"]["funding_usdt"] is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"close_order_count": 2}, "MULTIPLE_OR_UNKNOWN_CLOSE_ORDERS"),
        ({"close_order_count": None}, "MULTIPLE_OR_UNKNOWN_CLOSE_ORDERS"),
        ({"close_order_count": True}, "MULTIPLE_OR_UNKNOWN_CLOSE_ORDERS"),
        ({"native_close_count": 0}, "NOT_PROVEN_NATIVE_STOP_EXIT"),
        ({"native_close_count": True}, "NOT_PROVEN_NATIVE_STOP_EXIT"),
        ({"native_stop_type_verified": False}, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({"native_stop_type_verified": 1}, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({"native_trigger_price": None}, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        ({"native_trigger_price": "NaN"}, "NATIVE_STOP_TRIGGER_UNVERIFIED"),
        (
            {"native_trigger_price": "0.164"},
            "NATIVE_STOP_TRIGGER_DIFFERS_FROM_INITIAL_PLAN",
        ),
    ],
)
def test_partial_reductions_non_stop_and_unproven_trigger_not_stop_slippage(
    changes, reason
):
    trade, accounting, plan = fixture()
    case = build_execution_cost_case(trade, accounting, {**plan, **changes})
    assert case["status"] == "AVAILABLE"
    assert case["stop_decomposition"] == {"status": "NOT_COMPARABLE", "reason": reason}
    assert (
        case["price_comparison"]["exit_reference_is_not_claimed_actual_trigger"] is True
    )


def test_partial_open_uses_actual_filled_quantity_not_requested_stop_budget():
    trade, accounting, plan = fixture()
    plan.update(intent_quantity="1000", sizing_quantity="1000", result_quantity="1000")
    case = build_execution_cost_case(trade, accounting, plan)
    assert case["plan"]["actual_quantity_equals_requested"] is False
    assert case["plan"]["gross_stop_loss_requested_quantity_usdt"] == "7.9000"
    assert case["plan"]["gross_stop_loss_actual_quantity_usdt"] == "5.6801"
    assert case["stop_decomposition"]["identity_verified"] is True


def test_multiple_fills_weighted_vwap_does_not_round_arithmetic_identity():
    trade, accounting, plan = fixture()
    # Three units at unequal prices may have non-terminating average prices.
    trade.update(gross_usdt="-8", net_usdt="-8.2", recorded_fees_usdt="0.2")
    accounting.update(
        opening_quantity="3",
        closing_quantity="3",
        opening_notional="301",
        closing_notional="293",
    )
    plan.update(
        planned_entry_price="100",
        initial_stop_price="98",
        intent_quantity="3",
        sizing_quantity="3",
        result_quantity="3",
        native_trigger_price="98",
    )
    with localcontext() as context:
        context.prec = 3
        case = build_execution_cost_case(trade, accounting, plan)
    assert case["stop_decomposition"]["identity_verified"] is True
    assert case["stop_decomposition"]["actual_gross_loss_minus_planned_usdt"] == "2"


def test_favorable_fill_deviations_are_signed_not_clipped_to_zero():
    trade, accounting, plan = fixture()
    trade.update(gross_usdt="-5.3925", net_usdt="-5.5")
    accounting.update(opening_notional="124.2432", closing_notional="118.8507")
    case = build_execution_cost_case(trade, accounting, plan)
    assert Decimal(case["price_comparison"]["entry_adverse_deviation_usdt"]) < 0
    assert (
        Decimal(
            case["price_comparison"]["exit_adverse_deviation_from_initial_stop_usdt"]
        )
        < 0
    )
    assert case["stop_decomposition"]["identity_verified"] is True


@pytest.mark.parametrize(
    "bad", [None, "NaN", "Infinity", True, 0.1729, "1e-100", "1e100", "SECRET"]
)
def test_bad_plan_numbers_unknown_without_serializing_values(bad):
    trade, accounting, plan = fixture()
    plan["planned_entry_price"] = bad
    case = build_execution_cost_case(trade, accounting, plan)
    assert case["status"] == "UNKNOWN" and case["reason"] == "FROZEN_PLAN_INVALID"
    assert case["actual"]["settled_net_usdt"] == "-6.161111"
    assert "SECRET" not in json.dumps(case)


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_present": False},
        {"result_action": "CLOSE"},
        {"result_side": "SELL"},
        {"sizing_reason": "NOT_SIZED"},
        {"sizing_quantity": "720"},
        {"result_quantity": "720"},
        {"intent_quantity": "718", "sizing_quantity": "718", "result_quantity": "718"},
        {"initial_stop_price": "0.2"},
    ],
)
def test_plan_binding_mismatch_cannot_explain_loss(changes):
    trade, accounting, plan = fixture()
    case = build_execution_cost_case(trade, accounting, {**plan, **changes})
    assert case["status"] == "UNKNOWN" and case["plan"] is None


def test_missing_cost_buffer_fees_and_budget_not_assumed_zero_or_absent():
    trade, accounting, plan = fixture()
    trade["recorded_fees_usdt"] = None
    plan.update(cost_buffer_fraction=None, budgeted_stop_risk_usdt=None)
    case = build_execution_cost_case(trade, accounting, plan)
    assert case["plan"]["frozen_cost_buffer_fraction"] is None
    assert case["plan"]["reference_notional_cost_buffer_estimate_usdt"] is None
    assert case["plan"]["recorded_budgeted_stop_risk_usdt"] is None
    assert case["actual"]["recorded_fees_usdt"] is None
    assert case["stop_decomposition"]["identity_verified"] is True


def test_unknown_plan_and_inconsistent_accounting_not_reconstructed_from_net():
    trade, accounting, _plan = fixture()
    missing = build_execution_cost_case(trade, accounting)
    assert missing["reason"] == "FROZEN_PLAN_UNAVAILABLE"
    assert missing["actual"]["entry_fill_vwap"] == "0.1732"
    bad = build_execution_cost_case(trade, {**accounting, "closing_quantity": "1"})
    assert bad["reason"] == "VERIFIED_ACCOUNTING_MISMATCH"
    assert "entry_fill_vwap" not in bad["actual"]
