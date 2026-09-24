from copy import deepcopy
from decimal import Decimal

from test_v2_directional import inputs, sizing

from v2_core.directional import evaluate_market, size_candidate


def test_score_policy_really_changes_admission_without_mutating_inputs():
    data = inputs()
    original = deepcopy(data)
    assert evaluate_market(**data).reason == "CANDIDATE"
    assert evaluate_market(**data, policy={"entry.min_score": 90}).reason != "CANDIDATE"
    assert data == original


def test_short_threshold_is_configurable():
    data = inputs("S8")
    data["event"]["strength"] = "55"
    assert evaluate_market(**data).reason == "strength_below_minimum"
    assert (
        evaluate_market(**data, policy={"entry.short_min_strength": "50"}).reason
        == "CANDIDATE"
    )


def test_leverage_and_sizing_are_configuration_driven():
    plan = evaluate_market(**inputs(), policy={"leverage.trend": 2})
    assert plan.leverage == 2
    normal = size_candidate(plan, **sizing())
    smaller = size_candidate(plan, **sizing(), policy={"sizing.max_allocation": "0.03"})
    assert Decimal(smaller.quantity) < Decimal(normal.quantity)


def test_seventy_percent_pool_stops_sizing_when_already_used():
    plan = evaluate_market(**inputs())
    result = size_candidate(
        plan,
        **sizing(used_pool_margin="700"),
        policy={"capital.enabled": True, "sizing.pool_fraction": "0.70"},
    )
    assert result.quantity == "0"
