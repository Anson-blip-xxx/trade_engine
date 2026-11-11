"""Pure closed-episode price/cost evidence, never sizing or execution authority.

The initial stop is a benchmark, not a universal exit target. Only a single
native stop close with its recorded trigger matched to the initial stop permits
a stop-loss decomposition. Multiple fills are quantity-weighted; multiple close
orders, changed stops and ordinary exits are not silently called stop slippage.
"""

from decimal import Decimal, InvalidOperation, localcontext

VERSION = "execution-cost-evidence-v1"
_LIMITATIONS = [
    "FROZEN_PLAN_IS_ESTIMATE_NOT_GUARANTEED_MAXIMUM_LOSS",
    "EXISTING_COST_BUFFER_AND_PRICE_ENVELOPE_NOT_ASSUMED_MISSING",
    "PRICE_DEVIATIONS_NOT_PROOF_OF_SPREAD_LATENCY_OR_LIQUIDITY_CAUSE",
    "INITIAL_STOP_COMPARISON_NOT_ACTUAL_TRIGGER_SLIPPAGE_WITHOUT_TRIGGER_PROOF",
    "WHOLE_EPISODE_COSTS_NOT_INCREMENTAL_COST_OF_CONTINUING_TO_HOLD",
    "SETTLED_NET_ALREADY_INCLUDES_RECORDED_COSTS_NO_SECOND_SUBTRACTION",
    "FUNDING_UNKNOWN_NOT_INFERRED_FROM_GROSS_NET_AND_FEES",
]


def _number(value):
    if type(value) not in (str, int, Decimal) or len(str(value)) > 100:
        raise ValueError("INVALID_COST_NUMBER")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("INVALID_COST_NUMBER") from None
    if (
        not number.is_finite()
        or abs(number.as_tuple().exponent) > 40
        or len(number.as_tuple().digits) > 60
        or number.copy_abs() > Decimal("1e18")
    ):
        raise ValueError("INVALID_COST_NUMBER")
    return number


def _text(value):
    return None if value is None else format(value, "f") if value else "0"


def _optional(value):
    try:
        return _number(value)
    except ValueError:
        return None


def build_execution_cost_case(trade, accounting, plan=None):
    """Use an already verified settlement and detached allowlisted projections.

    No raw identities/configuration are exported. A missing plan preserves the
    verified realized values; it never creates zero fees or a zero risk budget.
    """
    case = {
        "trade_ref": trade["trade_ref"],
        "symbol": trade["symbol"],
        "side": trade["side"],
        "status": "UNKNOWN",
        "reason": "FROZEN_PLAN_UNAVAILABLE",
        "actual": {
            "gross_usdt": trade["gross_usdt"],
            "settled_net_usdt": trade["net_usdt"],
            "recorded_fees_usdt": trade["recorded_fees_usdt"],
            "funding_usdt": None,
            "funding_status": "NOT_SEPARATELY_LOADED",
        },
        "plan": None,
        "price_comparison": None,
        "stop_decomposition": {
            "status": "UNKNOWN",
            "reason": "FROZEN_PLAN_UNAVAILABLE",
        },
    }
    with localcontext() as context:
        context.prec = 100
        try:
            qty, closing, opening_notional, closing_notional = (
                _number(accounting[key])
                for key in (
                    "opening_quantity",
                    "closing_quantity",
                    "opening_notional",
                    "closing_notional",
                )
            )
            sign = 1 if trade["side"] == "BUY" else -1
            if (
                trade["side"] not in {"BUY", "SELL"}
                or qty <= 0
                or qty != closing
                or opening_notional <= 0
                or closing_notional <= 0
                or (closing_notional - opening_notional) * sign
                != _number(trade["gross_usdt"])
            ):
                raise ValueError("VERIFIED_ACCOUNTING_MISMATCH")
        except (ValueError, TypeError, KeyError):
            case["reason"] = "VERIFIED_ACCOUNTING_MISMATCH"
            return case
        case["actual"].update(
            opening_quantity=_text(qty),
            closing_quantity=_text(closing),
            entry_fill_vwap=_text(opening_notional / qty),
            exit_fill_vwap=_text(closing_notional / qty),
            price_basis="QUANTITY_WEIGHTED_ACTUAL_FILLS_SAME_ENVIRONMENT",
        )
        if not isinstance(plan, dict):
            return case
        try:
            entry, stop, requested, sized, result_qty = (
                _number(plan.get(key))
                for key in (
                    "planned_entry_price",
                    "initial_stop_price",
                    "intent_quantity",
                    "sizing_quantity",
                    "result_quantity",
                )
            )
            if (
                plan.get("evidence_present") is not True
                or plan.get("result_action") != "OPEN"
                or plan.get("result_side") != trade["side"]
                or plan.get("sizing_reason") != "SIZED"
                or min(entry, stop, requested) <= 0
                or requested != sized
                or requested != result_qty
                or qty > requested
                or sign * (entry - stop) <= 0
            ):
                raise ValueError("FROZEN_PLAN_INVALID")
        except (ValueError, TypeError):
            case["reason"] = "FROZEN_PLAN_INVALID"
            case["stop_decomposition"]["reason"] = "FROZEN_PLAN_INVALID"
            return case
        buffer = _optional(plan.get("cost_buffer_fraction"))
        budget = _optional(plan.get("budgeted_stop_risk_usdt"))
        if buffer is not None and not 0 <= buffer <= Decimal("0.1"):
            buffer = None
        if budget is not None and budget < 0:
            budget = None
        loss_unit = sign * (entry - stop)
        # Calculate in notionals: VWAP display division must never perturb the
        # exact arithmetic identity used to explain loss above the reference.
        entry_deviation = sign * (opening_notional - entry * qty)
        close_deviation = sign * (stop * qty - closing_notional)
        case.update(status="AVAILABLE", reason=None)
        case["plan"] = {
            "entry_reference_price": _text(entry),
            "initial_stop_price": _text(stop),
            "requested_quantity": _text(requested),
            "actual_quantity_equals_requested": qty == requested,
            "gross_stop_loss_requested_quantity_usdt": _text(requested * loss_unit),
            "gross_stop_loss_actual_quantity_usdt": _text(qty * loss_unit),
            "frozen_cost_buffer_fraction": _text(buffer),
            "reference_notional_cost_buffer_estimate_usdt": _text(
                entry * requested * buffer
            )
            if buffer is not None
            else None,
            "recorded_budgeted_stop_risk_usdt": _text(budget),
            "cost_buffer_basis": "FROZEN_SIZING_EXPLANATION_NOT_REALIZED_FEES",
        }
        case["price_comparison"] = {
            "entry_adverse_deviation_usdt": _text(entry_deviation),
            "exit_adverse_deviation_from_initial_stop_usdt": _text(close_deviation),
            "basis": "ACTUAL_FILLED_QUANTITY_SIGNED_REFERENCE_DIFFERENCES",
            "positive_means_adverse": True,
            "exit_reference_is_not_claimed_actual_trigger": True,
        }
        count, native = plan.get("close_order_count"), plan.get("native_close_count")
        trigger = _optional(plan.get("native_trigger_price"))
        reason = (
            "MULTIPLE_OR_UNKNOWN_CLOSE_ORDERS"
            if type(count) is not int or count != 1
            else "NOT_PROVEN_NATIVE_STOP_EXIT"
            if type(native) is not int or native != 1
            else "NATIVE_STOP_TRIGGER_UNVERIFIED"
            if plan.get("native_stop_type_verified") is not True or trigger is None
            else "NATIVE_STOP_TRIGGER_DIFFERS_FROM_INITIAL_PLAN"
            if trigger != stop
            else None
        )
        case["stop_decomposition"] = {
            "status": "AVAILABLE" if reason is None else "NOT_COMPARABLE",
            "reason": reason,
        }
        if reason is None:
            case["stop_decomposition"].update(
                basis="SINGLE_NATIVE_STOP_ORDER_ACTUAL_QUANTITY_INITIAL_TRIGGER_MATCHED",
                planned_gross_loss_usdt=_text(qty * loss_unit),
                actual_gross_loss_usdt=_text(-_number(trade["gross_usdt"])),
                entry_adverse_deviation_usdt=_text(entry_deviation),
                stop_fill_adverse_deviation_usdt=_text(close_deviation),
                actual_gross_loss_minus_planned_usdt=_text(
                    entry_deviation + close_deviation
                ),
                identity_verified=(
                    qty * loss_unit + entry_deviation + close_deviation
                    == -_number(trade["gross_usdt"])
                ),
                fees_excluded_from_price_decomposition=True,
            )
    return case


def empty_execution_cost_review():
    return {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": "VERIFIED_SETTLEMENT_POPULATION_UNAVAILABLE",
        "research_only": True,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "basis": "FROZEN_ENTRY_PLAN_VS_VERIFIED_WHOLE_EPISODE_SETTLEMENT",
        "denominator_settled_count": None,
        "plan_comparable_count": 0,
        "native_stop_decomposed_count": 0,
        "cases": [],
        "coverage": {
            "query_complete": False,
            "queried_episode_count": 0,
            "evidence_row_count": 0,
            "unknown_count": None,
            "retryable": False,
        },
        "limitations": list(_LIMITATIONS),
    }
