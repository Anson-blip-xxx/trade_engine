"""Strict normalized leverage evidence and exact derived capital boundaries."""

from copy import deepcopy
from decimal import Decimal, Inexact, localcontext

import pytest
from test_v2_strategy_capital import SCOPE, material

from v2_core.strategy_capital import (
    CAPITAL_EVIDENCE_SCHEMA,
    read_strategy_capital,
)


def proof():
    return {
        "selected": 3,
        "checks": {"liquidity": True, "funding": False},
        "venue": {
            "symbol": "BTCUSDT",
            "observed_at_ms": 1,
            "book_at_ms": 2,
            "spread_fraction": "0.0002000200020002000200020002",
            "top_notional": "100000.0000000000000000000000000001",
            "tiers": [
                {"floor": "0", "cap": "1000", "max_leverage": 20, "mmr": "0.01"},
                {"floor": "1000", "cap": "1000000", "max_leverage": 10, "mmr": "0.02"},
            ],
        },
    }


def record(candidate=None, *, plan_changes=None):
    _, stored = material(
        plan_changes=plan_changes,
        features={
            "capital": {
                "schema": CAPITAL_EVIDENCE_SCHEMA,
                "leverage_decision": proof() if candidate is None else candidate,
            }
        },
    )
    return stored


def test_normalized_evidence_keeps_derived_digits_without_claiming_a_permit():
    stored = record()
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        projected = read_strategy_capital(stored, scope=SCOPE)
    assert projected.leverage_proof == proof()
    assert projected.plan.execution_authorized is False
    # Neither a model confidence nor selected leverage is an execution permit.
    assert projected.leverage_proof["checks"]["funding"] is False


@pytest.mark.parametrize(
    "path,value",
    [
        (("selected",), True),
        (("selected",), "3"),
        (("selected",), 8),
        (("venue",), None),
        (("venue",), []),
        (("checks",), None),
        (("checks",), []),
        (("checks", "liquidity"), 1),
        (("checks", "liquidity"), "true"),
        (("venue", "symbol"), "ETHUSDT"),
        (("venue", "observed_at_ms"), True),
        (("venue", "observed_at_ms"), "1"),
        (("venue", "observed_at_ms"), 0),
        (("venue", "observed_at_ms"), 3),
        (("venue", "book_at_ms"), 0),
        (("venue", "book_at_ms"), 3),
        (("venue", "book_at_ms"), False),
        (("venue", "spread_fraction"), "-0.0001"),
        (("venue", "spread_fraction"), "NaN"),
        (("venue", "spread_fraction"), "Infinity"),
        (("venue", "spread_fraction"), True),
        (("venue", "spread_fraction"), 0.001),
        (("venue", "top_notional"), "0"),
        (("venue", "top_notional"), "-1"),
        (("venue", "top_notional"), "NaN"),
        (("venue", "top_notional"), "Infinity"),
        (("venue", "tiers"), []),
        (("venue", "tiers"), {}),
        (("venue", "tiers", 0), None),
        (("venue", "tiers", 0, "mmr"), "-1"),
        (("venue", "tiers", 0, "mmr"), "0"),
        (("venue", "tiers", 0, "mmr"), "1"),
        (("venue", "tiers", 0, "mmr"), "NaN"),
        (("venue", "tiers", 0, "mmr"), "Infinity"),
        (("venue", "tiers", 0, "mmr"), True),
        (("venue", "tiers", 0, "floor"), "-1"),
        (("venue", "tiers", 0, "floor"), "1"),
        (("venue", "tiers", 0, "cap"), "0"),
        (("venue", "tiers", 0, "cap"), "NaN"),
        (("venue", "tiers", 0, "max_leverage"), True),
        (("venue", "tiers", 0, "max_leverage"), "20"),
        (("venue", "tiers", 0, "max_leverage"), 0),
        (("venue", "tiers", 0, "max_leverage"), 126),
        (("venue", "tiers", 1, "floor"), "999"),
        (("venue", "tiers", 1, "floor"), "1001"),
    ],
)
def test_present_bad_evidence_always_fails_closed_even_static_low_leverage(path, value):
    candidate = proof()
    target = candidate
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="^STRATEGY_CAPITAL_EVIDENCE_INVALID$"):
        read_strategy_capital(record(candidate), scope=SCOPE)


@pytest.mark.parametrize(
    "path",
    [
        ("venue",),
        ("checks",),
        ("venue", "observed_at_ms"),
        ("venue", "book_at_ms"),
        ("venue", "spread_fraction"),
        ("venue", "top_notional"),
        ("venue", "tiers"),
        ("venue", "tiers", 0, "floor"),
        ("venue", "tiers", 0, "cap"),
        ("venue", "tiers", 0, "mmr"),
        ("venue", "tiers", 0, "max_leverage"),
    ],
)
def test_incomplete_normalized_evidence_has_fixed_error(path):
    candidate = proof()
    target = candidate
    for name in path[:-1]:
        target = target[name]
    del target[path[-1]]
    with pytest.raises(ValueError, match="^STRATEGY_CAPITAL_EVIDENCE_INVALID$"):
        read_strategy_capital(record(candidate), scope=SCOPE)


def test_omitted_optional_proof_is_distinct_from_a_present_empty_proof():
    _, without = material()
    assert read_strategy_capital(without, scope=SCOPE).leverage_proof is None
    with pytest.raises(ValueError, match="^STRATEGY_CAPITAL_EVIDENCE_INVALID$"):
        read_strategy_capital(record({}), scope=SCOPE)


def test_valid_historical_proof_remains_readable_after_entry_expiry():
    projected = read_strategy_capital(record(), scope=SCOPE)
    assert not projected.plan.entry_valid_at(projected.plan.expires_at_ms)
    assert projected.leverage_proof == proof()
    assert projected.stop_risk("250", current_cost_fraction="0.002") > 0


def test_derived_quantity_price_product_beyond_storage_scale_is_not_a_bad_fact():
    _, stored = material(
        plan_changes={
            "quantity": "0.075",
            "quantity_step": "0.025",
            "entry_price": "0.0000000000000003",
            "stop_price": "0.0000000000000001",
            "planned_loss": "0.000000000000000015",
        }
    )
    projected = read_strategy_capital(stored, scope=SCOPE)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result = projected.stop_risk(Decimal("2.25e-17"), current_cost_fraction="0.002")
    assert result == Decimal("0.000000000000000016")
    assert projected.plan.entry_price == "0.0000000000000003"
    assert projected.plan.quantity == "0.075"


@pytest.mark.parametrize(
    "notional",
    ["NaN", "Infinity", "1e20", "1e-73", "1e-99999999", "x", 2.25e-17, True],
)
def test_derived_notional_bounds_do_not_accept_unbounded_or_non_decimal_values(
    notional,
):
    projected = read_strategy_capital(record(), scope=SCOPE)
    with pytest.raises(ValueError, match="^STRATEGY_CAPITAL_RISK_INPUT_INVALID$"):
        projected.stop_risk(notional, current_cost_fraction="0.002")


def test_derived_scale_allowance_does_not_weaken_cost_fact_validation():
    projected = read_strategy_capital(record(), scope=SCOPE)
    with pytest.raises(ValueError, match="^STRATEGY_CAPITAL_RISK_INPUT_INVALID$"):
        projected.stop_risk("250", current_cost_fraction="0.0000000000000000001")


def test_caller_mutation_cannot_change_normalized_proof():
    candidate = proof()
    expected = deepcopy(candidate)
    projected = read_strategy_capital(record(candidate), scope=SCOPE)
    candidate["venue"]["tiers"][0]["mmr"] = "-1"
    candidate["checks"]["liquidity"] = False
    assert projected.leverage_proof == expected
