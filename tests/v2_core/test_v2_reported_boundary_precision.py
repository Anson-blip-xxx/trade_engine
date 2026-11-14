"""Reported boundary cash is opt-in evidence, never a rounding epsilon."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, Inexact, localcontext

import pytest
from test_v2_observed_entry_precision import anchor, inventory
from test_v2_precision_contract import SCOPE, cash_case

from v2_core.evidence import canonical, digest
from v2_core.settlement_precision import REPORTED_BOUNDARY_MODEL, rounding_proof


def boundary_case(
    *, side="BUY", source="directional-exit-v1", quantity="2997", symbol="QAUSDT"
):
    entry, close = (".03843", ".03844") if side == "BUY" else (".03844", ".03843")
    with localcontext() as ctx:
        ctx.prec = 100
        pnl = str(Decimal(quantity) * Decimal(".00001") - Decimal(".00000001"))
    trace, report, _ = cash_case(side, [(quantity, entry)], [(quantity, close, pnl)])
    trace["request"]["symbol"] = symbol
    observed = inventory(
        SCOPE,
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry=entry,
        updated=3,
        start=5,
        end=6,
    )
    request, witness = anchor(
        SCOPE,
        trace["intent_id"],
        trace["orders"][0]["order_id"],
        observed,
        source=source,
    )
    trace["orders"][-1]["request_evidence"] = request
    return trace, report, {trace["orders"][-1]["order_id"]: witness}


def prove(trace, report, witnesses, **kwargs):
    return rounding_proof(
        trace,
        report,
        entry_observations=witnesses,
        allow_reported_boundary_cash=True,
        **kwargs,
    )


@pytest.mark.parametrize("precision", [6, 28, 100])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("source", ["directional-exit-v1", "unprotected-emergency-v1"])
def test_reported_positive_boundary_is_candidate_only(precision, side, source):
    trace, report, witnesses = boundary_case(side=side, source=source)
    original = deepcopy((trace, report, witnesses))
    with localcontext() as ctx:
        ctx.prec = precision
        ctx.traps[Inexact] = True
        proof = prove(trace, report, witnesses)
    assert proof["model"] == REPORTED_BOUNDARY_MODEL
    assert proof["amount"] == "-0.00000001"
    assert Decimal(proof["theoretical_gross_pnl"]) == Decimal(".02997")
    assert proof["venue_reported_gross_pnl"] == "0.02996999"
    assert proof["algorithm_verified"] is False
    assert proof["requires_final_wallet_match"] is True
    assert proof["settlement_authorized"] is False
    fill = proof["fills"][0]
    assert fill["method"] == "VENUE_REPORTED_BOUNDARY_CASH_ADJUSTMENT"
    assert fill["cash_difference"] == "-0.00000001"
    assert fill["cash_quantum"] == "0.00000001"
    assert fill["algorithm_verified"] is False
    assert fill["requires_final_wallet_match"] is True
    assert fill["settlement_authorized"] is False
    assert fill["entry_observation"]["entry_price"] in {".03843", ".03844"}
    assert (trace, report, witnesses) == original


@pytest.mark.parametrize("quantity", ["1", "100000", ".125"])
@pytest.mark.parametrize("symbol", ["OTHERUSDT", "1000QAUSDT"])
def test_shape_is_not_an_account_symbol_or_quantity_exception(quantity, symbol):
    trace, report, witnesses = boundary_case(quantity=quantity, symbol=symbol)
    assert prove(trace, report, witnesses)["amount"] == "-0.00000001"


def test_neighbor_remains_rejected_without_explicit_opt_in():
    trace, report, witnesses = boundary_case()
    for kwargs in ({}, {"allow_reported_boundary_cash": False}):
        with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
            rounding_proof(trace, report, entry_observations=witnesses, **kwargs)


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_opt_in_must_be_an_explicit_boolean(invalid):
    trace, report, witnesses = boundary_case()
    with pytest.raises(ValueError, match="EXPLICIT_BOUNDARY_CASH_POLICY_REQUIRED"):
        rounding_proof(
            trace,
            report,
            entry_observations=witnesses,
            allow_reported_boundary_cash=invalid,
        )


@pytest.mark.parametrize("witnesses", [None, {}])
def test_reported_cash_cannot_supply_missing_entry_evidence(witnesses):
    trace, report, _ = boundary_case()
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        prove(trace, report, witnesses)


def test_exact_one_quantum_profit_cannot_be_turned_into_zero():
    trace, report, witnesses = boundary_case(quantity=".001")
    assert Decimal(report["gross_pnl"]) == Decimal(".00000001")
    assert Decimal(trace["fills"][-1]["evidence"]["venue_realized_pnl"]) == 0
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        prove(trace, report, witnesses)


def test_valid_rounded_entry_cannot_supply_boundary_adjustment():
    trace, report, witnesses = boundary_case()
    # The recorded .03843 remains a valid nearest pre-close entry, with a
    # whole-position difference below one cash quantum. It is nevertheless
    # not the exact fill-derived basis required by this new candidate class.
    trace["fills"][0]["price"] = ".0384300000001"
    trace["fills"][-1]["price"] = ".0384400000001"
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        prove(trace, report, witnesses)


@pytest.mark.parametrize(
    "closing,observed",
    [
        (".03844", ".02997001"),  # Opposite cash discrepancy.
        (".03844", ".02996998"),  # Two quanta.
        (".03844", ".029969995"),  # Half a quantum.
        (".038440000001", ".029969992997"),  # Exactly one quantum but off-grid.
        (".03843", "-.00000001"),  # Zero theoretical return.
        (".03842", "-.02997001"),  # Negative, further from zero.
        (".03842", "-.02996999"),  # Negative, closer to zero.
        (".03844", "0"),
        (".03844", "-.02996999"),
    ],
)
def test_unobserved_boundary_shapes_are_not_generalized(closing, observed):
    trace, report, witnesses = boundary_case()
    trace["fills"][-1]["price"] = closing
    trace["fills"][-1]["evidence"]["venue_realized_pnl"] = observed
    report["gross_pnl"] = str((Decimal(closing) - Decimal(".03843")) * 2997)
    with pytest.raises(ValueError, match="UNEXPLAINED_FILL_PNL_PRECISION"):
        prove(trace, report, witnesses)


@pytest.mark.parametrize("which", ["opening", "closing", "order"])
def test_multiple_fills_or_extra_orders_cannot_use_boundary_candidate(which):
    trace, report, witnesses = boundary_case()
    if which == "order":
        trace["orders"].append(
            {
                "order_id": "cancelled-close",
                "leg": "CLOSE",
                "status": "CANCELLED",
                "quantity": "2997",
            }
        )
    else:
        index = 0 if which == "opening" else -1
        fill = trace["fills"][index]
        sibling = deepcopy(fill)
        fill["quantity"] = "999"
        sibling["quantity"] = "1998"
        sibling["evidence"]["trade_id"] = "3"
        if which == "closing":
            fill["evidence"]["venue_realized_pnl"] = ".00998999"
            sibling["evidence"]["venue_realized_pnl"] = ".01998"
        trace["fills"].append(sibling)
    with pytest.raises(ValueError):
        prove(trace, report, witnesses)


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "environment",
        "symbol",
        "currency",
        "mode",
        "producer",
        "open_quantity",
        "close_quantity",
        "open_status",
        "close_status",
        "digest",
        "response_digest",
        "stale_witness",
        "quantity",
        "entry",
        "state_id",
        "report",
        "opening_realized_pnl",
    ],
)
def test_original_identity_economics_and_witness_fences_remain(defect):
    trace, report, witnesses = boundary_case()
    witness = next(iter(witnesses.values()))
    observed = witness["inventory"]
    if defect == "scope":
        observed["account_scope"]["account_id"] = "another-account"
    elif defect == "environment":
        trace["scope"]["environment"] = "LIVE"
    elif defect == "symbol":
        trace["request"]["symbol"] = "QAUSDC"
    elif defect == "currency":
        report["settlement_currency"] = "USDC"
    elif defect == "mode":
        trace["configuration"]["mode"] = "LIVE"
    elif defect == "producer":
        trace["scope"]["producer"] = "other"
    elif defect in {"open_quantity", "close_quantity"}:
        trace["orders"][0 if defect == "open_quantity" else -1]["quantity"] = "2998"
    elif defect in {"open_status", "close_status"}:
        trace["orders"][0 if defect == "open_status" else -1]["status"] = "ACKNOWLEDGED"
    elif defect == "digest":
        witness["exit_state"]["preflight_digest"] = "0" * 64
    elif defect == "response_digest":
        observed["response_digests"]["positions_before"] = "0" * 64
    elif defect == "stale_witness":
        observed["finished_at_ms"] = trace["fills"][-1]["occurred_at_ms"]
    elif defect in {"quantity", "entry"}:
        for name in ("positions_before", "positions_after"):
            observed["responses"][name][0][
                "positionAmt" if defect == "quantity" else "entryPrice"
            ] = "2998" if defect == "quantity" else ".0384300000001"
    elif defect == "state_id":
        trace["orders"][-1]["request_evidence"]["state_id"] = trace["intent_id"]
    elif defect == "report":
        report["gross_pnl"] = ".02996999"
    elif defect == "opening_realized_pnl":
        trace["fills"][0]["evidence"]["venue_realized_pnl"] = ".00000001"
    if defect != "response_digest":
        observed["response_digests"] = {
            name: digest(canonical({"response": value}))
            for name, value in observed["responses"].items()
        }
    if defect != "digest":
        witness["exit_state"]["preflight_digest"] = digest(canonical(observed))
    with pytest.raises(ValueError):
        prove(trace, report, witnesses)


def test_distinct_account_can_qualify_only_with_its_own_exact_witness():
    trace, report, _ = boundary_case()
    scope = replace(SCOPE, account_id="different-test-account")
    trace["scope"]["account_id"] = scope.account_id
    observed = inventory(
        scope,
        symbol="QAUSDT",
        side="BUY",
        quantity="2997",
        entry=".03843",
        updated=3,
        start=5,
        end=6,
    )
    request, witness = anchor(scope, trace["intent_id"], "open", observed)
    trace["orders"][-1]["request_evidence"] = request
    proof = prove(trace, report, {trace["orders"][-1]["order_id"]: witness})
    assert proof["model"] == REPORTED_BOUNDARY_MODEL
