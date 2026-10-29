from dataclasses import asdict, replace
from decimal import Decimal
from fractions import Fraction

import pytest
from test_v2_directional import inputs, sizing

from v2_core.capital_model import rehearsal_profile
from v2_core.directional import evaluate_market, size_candidate
from v2_core.runtime_policy import resolve
from v2_core.strategy_controls import budget_examples, validate_changes
from v2_core.strategy_observations import observe_excursions


@pytest.mark.parametrize("side", ["S6", "S8"])
@pytest.mark.parametrize(
    "available,used,atr,drawdown,analysis",
    [
        ("1000", "0", "1", "1", "1"),
        ("1", "0", "1", "1", "1"),
        ("1000", "690", "1", "1", "1"),
        ("1000", "700", "1", "1", "1"),
        ("1000", "0", "10", "0.5", "0.5"),
        ("1000", "0", "1", "0", "1"),
    ],
)
def test_explanation_does_not_change_sizing(
    side, available, used, atr, drawdown, analysis
):
    policy = resolve(rehearsal_profile("1000"))
    plan = evaluate_market(**inputs(side), policy=policy)
    args = sizing(
        available_margin=available,
        used_pool_margin=used,
        atr_pct=atr,
        drawdown_factor=drawdown,
        analysis_factor=analysis,
    )
    details = {}
    expected = size_candidate(plan, **args, policy=policy)
    actual = size_candidate(plan, **args, policy=policy, explanation=details)
    assert asdict(actual) == asdict(expected)
    assert details["reason"] == actual.reason
    minimum = min(Fraction(v) for v in details["margin_limits"].values())
    assert details["binding_margin_limits"] == [
        k for k, v in details["margin_limits"].items() if Fraction(v) == minimum
    ]
    assert not details["execution_authorized"]


def test_tight_and_wide_stop_have_different_binding_constraints():
    settings = resolve(rehearsal_profile("1000"))
    before = budget_examples(settings)
    after = budget_examples(
        validate_changes(settings, {"sizing.max_margin_fraction": "0.15"})
    )
    assert before["rows"][0]["explanation"]["binding_margin_limits"] == [
        "SINGLE_MARGIN_CAP"
    ]
    # Existing allocation remains a constraint; raising one cap need not reach risk-sized 138 U.
    assert after["rows"][0]["explanation"]["binding_margin_limits"] == [
        "SCORE_ALLOCATION_AFTER_VOLATILITY"
    ]
    assert before["rows"][2]["result"] == after["rows"][2]["result"]
    assert before["rows"][2]["explanation"]["binding_margin_limits"] == [
        "STOP_RISK_CAP"
    ]
    assert Decimal(before["rows"][0]["result"]["notional"]) < Decimal(
        after["rows"][0]["result"]["notional"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"capital.enabled": False},
        {"quant.admission_enabled": True},
        {"capital.pool_fraction": "0.8", "sizing.pool_fraction": "0.8"},
        {"sizing.risk_fraction": "0.04"},
        {"sizing.max_margin_fraction": "0.71"},
        {"sizing.max_margin_fraction": ".0500"},
        {"sizing.max_margin_fraction": "NaN"},
        {"sizing.max_margin_fraction": 0.1},
    ],
)
def test_console_rejects_unsafe_or_unsupported_changes(changes):
    with pytest.raises(ValueError):
        validate_changes(resolve(rehearsal_profile("1000")), changes)


def test_sampled_excursions_are_persistent_idempotent_and_not_full_path():
    first = observe_excursions(None, now_ms=10, return_pct="-2")
    second = observe_excursions(first, now_ms=20, return_pct="5")
    third = observe_excursions(second, now_ms=30, return_pct="1")
    assert third["mfe_return_pct"] == "5" and third["mae_return_pct"] == "-2"
    assert third["samples"] == 3 and third["first_observed_ms"] == 10
    assert third["coverage"] == "SAMPLED_MARKS_NOT_FULL_PRICE_PATH"
    assert observe_excursions(third, now_ms=30, return_pct="1") == third
    with pytest.raises(ValueError, match="CLOCK_REGRESSION"):
        observe_excursions(third, now_ms=29, return_pct="0")


def test_invalid_stop_tick_is_explained_without_changing_result():
    plan = replace(evaluate_market(**inputs()), stop_fraction="0.001")
    details = {}
    result = size_candidate(plan, **sizing(price_tick="100"), explanation=details)
    assert result.reason == details["reason"] == "INVALID_STOP_TICK"
