"""Pure integration of frozen strategy plans with candidate/portfolio budgets.

No database, exchange, running configuration, or service is accessed. The fake
joined rows retain the real SQL column shapes used by each capital consumer.
"""

from copy import deepcopy
from decimal import ROUND_CEILING, Decimal, Inexact, localcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_strategy_capital import SCOPE, material

from v2_core.adaptive_leverage import BOOST_CHECKS
from v2_core.capital_model import guard_risk, portfolio_budget, rehearsal_profile
from v2_core.runtime_policy import resolve
from v2_core.strategy_capital import CAPITAL_EVIDENCE_SCHEMA, read_strategy_capital


def settings(**changes):
    return {
        **resolve(rehearsal_profile("1000")),
        "sizing.risk_fraction": "0.02",
        "sizing.max_margin_fraction": "0.1",
        "portfolio_risk.max_fraction": "0.1",
        "entry.max_adverse_price_fraction": "0.01",
        "leverage.adaptive_enabled": False,
        "leverage.low": 2,
        "leverage.medium": 3,
        "leverage.adaptive_high": 5,
        "leverage.boost": 8,
        **changes,
    }


CAPITAL = {"base": "1000", "factor": "1"}


def candidate(record, *, original=None, legacy=None):
    return (
        legacy,
        None,
        "BTCUSDT",
        None,
        None,
        "2.5",
        None,
        record,
        original,
    )


def held(record, *, original=None, notional="250"):
    record = deepcopy(record)
    strategy = read_strategy_capital(record, scope=SCOPE)
    original = (
        original
        if original is not None
        else str(strategy.stop_risk(notional, current_cost_fraction="0.002"))
    )
    with localcontext() as context:
        context.prec = 100
        margin = (
            Decimal(strategy.plan.quantity)
            * Decimal(strategy.plan.entry_price)
            / strategy.plan.leverage
        ).quantize(Decimal("1e-18"), rounding=ROUND_CEILING)
    record["reservation_capital"] = {
        "strategy_plan_digest": strategy.plan.plan_digest,
        "proposed_stop_risk": original,
        "reserved_margin": str(margin),
    }
    return (notional, None, original, "2.5", None, None, None, None, record)


def legacy_held(*, original=None):
    return ("100", "0.05", original, "1", "95", "LONG", None, None, None)


class Rows:
    def __init__(self, *, candidate_row=None, held_rows=()):
        self.candidate = candidate_row
        self.held = held_rows
        self.calls = []

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))
        return SimpleNamespace(
            fetchone=lambda: self.candidate, fetchall=lambda: self.held
        )


def guard(
    *,
    record=None,
    original=None,
    held_rows=(),
    notional="250",
    leverage=3,
    policy=None,
    capital=None,
):
    if record is None:
        _, record = material()
    conn = Rows(candidate_row=candidate(record, original=original), held_rows=held_rows)
    result = guard_risk(
        conn,
        SCOPE,
        episode=str(uuid4()),
        notional=Decimal(notional),
        leverage=leverage,
        settings=policy if policy is not None else settings(),
        capital=capital if capital is not None else CAPITAL,
    )
    return result, conn


def eight_x_record():
    proof = {
        "selected": 8,
        "checks": {name: True for name in BOOST_CHECKS},
        "venue": {
            "symbol": "BTCUSDT",
            "observed_at_ms": 1,
            "book_at_ms": 1,
            "spread_fraction": "0.0001",
            "top_notional": "100000",
            "tiers": [
                {"floor": "0", "cap": "1000000", "max_leverage": 20, "mmr": "0.005"}
            ],
        },
    }
    _, record = material(
        plan_changes={"leverage": 8},
        features={
            "capital": {
                "schema": CAPITAL_EVIDENCE_SCHEMA,
                "leverage_decision": proof,
            }
        },
    )
    return record


def adaptive_settings(**changes):
    return settings(
        **{
            "leverage.adaptive_enabled": True,
            "leverage.boost_enabled": True,
            "leverage.max_margin_consumption": "0.8",
            "leverage.max_spread": "0.001",
            "leverage.depth_multiple": "3",
            **changes,
        }
    )


def test_mixed_new_and_legacy_held_budget_has_one_batch_query():
    _, record = material()
    conn = Rows(held_rows=[held(record), legacy_held()])
    result = portfolio_budget(conn, SCOPE, settings=settings(), capital=CAPITAL)
    assert Decimal(result["held_stop_risk"]) == Decimal("20.705")
    assert Decimal(result["portfolio_risk_limit"]) == 100
    assert Decimal(result["remaining_stop_risk"]) == Decimal("79.295")
    assert len(conn.calls) == 1
    assert "LEFT JOIN v2_decision_evidence" in conn.calls[0][0]


def test_new_held_keeps_full_original_risk_even_with_partial_remaining_notional():
    _, record = material()
    conn = Rows(held_rows=[held(record, notional="1", original="30"), legacy_held()])
    result = portfolio_budget(conn, SCOPE, settings=settings(), capital=CAPITAL)
    assert Decimal(result["held_stop_risk"]) == Decimal("35.2")
    assert Decimal(result["remaining_stop_risk"]) == Decimal("64.8")


def test_new_held_retains_original_higher_policy_cost_after_policy_cost_reduction():
    _, record = material()
    conn = Rows(held_rows=[held(record, original="17.525")])
    result = portfolio_budget(
        conn,
        SCOPE,
        settings=settings(**{"sizing.cost_buffer_fraction": "0.001"}),
        capital=CAPITAL,
    )
    assert Decimal(result["held_stop_risk"]) == Decimal("17.525")
    assert Decimal(result["remaining_stop_risk"]) == Decimal("82.475")


@pytest.mark.parametrize(
    "field,value",
    [
        ("strategy_plan_digest", "a" * 64),
        ("proposed_stop_risk", None),
        ("proposed_stop_risk", "0"),
        ("proposed_stop_risk", "-1"),
        ("proposed_stop_risk", "NaN"),
        ("proposed_stop_risk", "17.525"),
        ("reserved_margin", None),
        ("reserved_margin", "0"),
        ("reserved_margin", "-1"),
        ("reserved_margin", "NaN"),
        ("reserved_margin", "1e-19"),
        ("reserved_margin", "1e20"),
    ],
)
def test_typed_held_requires_bound_consistent_original_risk_and_margin(field, value):
    _, record = material()
    row = held(record)
    row[-1]["reservation_capital"][field] = value
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
        portfolio_budget(
            Rows(held_rows=[row]), SCOPE, settings=settings(), capital=CAPITAL
        )


@pytest.mark.parametrize("proof", [None, {}, [], "corrupt"])
def test_typed_held_missing_or_malformed_reservation_proof_fails_closed(proof):
    _, record = material()
    row = held(record)
    row[-1]["reservation_capital"] = proof
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
        portfolio_budget(
            Rows(held_rows=[row]), SCOPE, settings=settings(), capital=CAPITAL
        )


def test_typed_held_missing_reservation_capital_is_not_a_recomputed_floor():
    _, record = material()
    row = held(record)
    row[-1].pop("reservation_capital")
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
        portfolio_budget(
            Rows(held_rows=[row]), SCOPE, settings=settings(), capital=CAPITAL
        )


def test_typed_held_requires_original_risk_column_to_match_proof():
    _, record = material()
    row = held(record)
    for original in (None, "15.504"):
        broken = (*row[:2], original, *row[3:])
        with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RESERVATION_UNVERIFIED"):
            portfolio_budget(
                Rows(held_rows=[broken]), SCOPE, settings=settings(), capital=CAPITAL
            )


@pytest.mark.parametrize("original", ["0", "-1", "NaN", "Infinity"])
def test_invalid_original_held_risk_never_releases_budget(original):
    _, record = material()
    with pytest.raises(ValueError):
        portfolio_budget(
            Rows(held_rows=[held(record, original=original)]),
            SCOPE,
            settings=settings(),
            capital=CAPITAL,
        )


def test_corrupt_new_marker_never_uses_valid_legacy_held_columns():
    _, record = material()
    record["snapshot"]["strategy_plan"] = None
    row = (*legacy_held()[:8], record)
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
        portfolio_budget(
            Rows(held_rows=[row]), SCOPE, settings=settings(), capital=CAPITAL
        )


def test_corrupt_new_marker_never_uses_valid_legacy_candidate_plan():
    _, record = material()
    record["snapshot"]["strategy_plan"] = None
    conn = Rows(
        candidate_row=candidate(
            record,
            legacy={
                "stop_fraction": "0.05",
                "margin_mode": "ISOLATED",
                "leverage": 3,
            },
        )
    )
    with pytest.raises(ValueError, match="STRATEGY_PLAN_INTENT_MISMATCH"):
        guard_risk(
            conn,
            SCOPE,
            episode=str(uuid4()),
            notional=Decimal(250),
            leverage=3,
            settings=settings(),
            capital=CAPITAL,
        )
    assert len(conn.calls) == 1


@pytest.mark.parametrize("field", ["capital.enabled", "portfolio_risk.enabled"])
def test_typed_plan_requires_both_capital_risk_guards(field):
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_RISK_POLICY_REQUIRED"):
        guard(policy=settings(**{field: False}))


def test_seventy_percent_pool_is_allowed_not_thirty_percent_and_cannot_exceed():
    result, _ = guard(policy=settings(**{"capital.pool_fraction": "0.70"}))
    assert Decimal(result["proposed_stop_risk"]) == Decimal("15.505")
    with pytest.raises(ValueError, match="CAPITAL_RESERVE_FLOOR_REQUIRED"):
        guard(policy=settings(**{"capital.pool_fraction": "0.700000000000000001"}))


@pytest.mark.parametrize(
    "side,stop,boundary,beyond",
    [
        ("BUY", "95", "252.5", "252.500000000000000001"),
        ("SELL", "105", "247.5", "247.499999999999999999"),
    ],
)
def test_adverse_entry_drift_boundary_is_exact(side, stop, boundary, beyond):
    _, record = material(plan_changes={"side": side, "stop_price": stop})
    guard(record=record, notional=boundary)
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_ENTRY_PRICE_DRIFT"):
        guard(record=record, notional=beyond)


@pytest.mark.parametrize(
    "side,stop,notional",
    [
        ("BUY", "95", "237.5"),
        ("BUY", "95", "237.49"),
        ("SELL", "105", "262.5"),
        ("SELL", "105", "262.51"),
    ],
)
def test_candidate_stop_crossing_is_rejected_both_sides(side, stop, notional):
    _, record = material(plan_changes={"side": side, "stop_price": stop})
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_STOP_CROSSED"):
        guard(record=record, notional=notional)


@pytest.mark.parametrize(
    "side,stop,notional",
    [
        ("BUY", "95", "251.5"),
        ("SELL", "105", "248.5"),
    ],
)
def test_current_drift_policy_can_tighten_not_loosen_frozen_envelope(
    side, stop, notional
):
    _, record = material(plan_changes={"side": side, "stop_price": stop})
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_ENTRY_PRICE_DRIFT"):
        guard(
            record=record,
            notional=notional,
            policy=settings(**{"entry.max_adverse_price_fraction": "0.005"}),
        )
    # A later wider policy still cannot approve outside the frozen 1% envelope.
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_ENTRY_PRICE_DRIFT"):
        guard(
            record=record,
            notional="255" if side == "BUY" else "245",
            policy=settings(**{"entry.max_adverse_price_fraction": "0.05"}),
        )


@pytest.mark.parametrize("cost,expected", [("0.001", "15.505"), ("0.01", "17.525")])
def test_candidate_uses_frozen_cost_floor_and_current_higher_cost(cost, expected):
    result, _ = guard(policy=settings(**{"sizing.cost_buffer_fraction": cost}))
    assert Decimal(result["proposed_stop_risk"]) == Decimal(expected)


def test_original_candidate_reservation_is_a_floor_not_a_recomputed_discount():
    result, _ = guard(original="20", notional="245")
    assert Decimal(result["proposed_stop_risk"]) == 20
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        guard(original="20.000000000000000001", notional="245")


@pytest.mark.parametrize("original", ["0", "-1", "NaN", "Infinity"])
def test_invalid_original_candidate_risk_is_not_ignored(original):
    with pytest.raises(ValueError):
        guard(original=original)


def test_single_stop_risk_budget_exact_boundary():
    result, _ = guard(policy=settings(**{"sizing.risk_fraction": "0.015505"}))
    assert Decimal(result["proposed_stop_risk"]) == Decimal("15.505")
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        guard(policy=settings(**{"sizing.risk_fraction": "0.015504999999999999"}))


def test_single_margin_budget_exact_boundary():
    _, record = material(plan_changes={"leverage": 2})
    guard(
        record=record,
        leverage=2,
        policy=settings(**{"sizing.max_margin_fraction": "0.125"}),
    )
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        guard(
            record=record,
            leverage=2,
            policy=settings(**{"sizing.max_margin_fraction": "0.124999999999999999"}),
        )


def test_remaining_portfolio_budget_subtracts_held_risk_at_exact_boundary():
    result, conn = guard(
        held_rows=[legacy_held()],
        policy=settings(**{"portfolio_risk.max_fraction": "0.020705"}),
    )
    assert Decimal(result["remaining_stop_risk"]) == Decimal("15.505")
    assert len(conn.calls) == 2
    assert conn.calls[1][1][1] == conn.calls[0][1][0]
    assert conn.calls[1][1][1] == conn.calls[1][1][2]
    with pytest.raises(ValueError, match="CAPITAL_PORTFOLIO_RISK_EXCEEDED"):
        guard(
            held_rows=[legacy_held()],
            policy=settings(**{"portfolio_risk.max_fraction": "0.020704999999999999"}),
        )


def test_new_plan_leverage_must_match_frozen_value_and_allowlist():
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_LEVERAGE_MISMATCH"):
        guard(leverage=2)
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_LEVERAGE_MISMATCH"):
        guard(leverage=True)
    _, record = material(plan_changes={"leverage": 125})
    with pytest.raises(ValueError, match="STRATEGY_CAPITAL_LEVERAGE_MISMATCH"):
        guard(record=record, leverage=125, policy=adaptive_settings())


def test_frozen_margin_mode_must_meet_current_isolated_requirement():
    _, record = material(plan_changes={"margin_mode": "CROSSED"})
    with pytest.raises(ValueError, match="CAPITAL_ISOLATED_MARGIN_REQUIRED"):
        guard(record=record)


def test_eight_x_requires_adaptive_policy_and_complete_true_boost_evidence():
    record = eight_x_record()
    result, _ = guard(record=record, leverage=8, policy=adaptive_settings())
    assert Decimal(result["proposed_stop_risk"]) == Decimal("15.505")
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
        guard(record=record, leverage=8)
    for missing in BOOST_CHECKS:
        broken = deepcopy(record)
        broken["snapshot"]["features"]["capital"]["leverage_decision"]["checks"].pop(
            missing
        )
        with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
            guard(record=broken, leverage=8, policy=adaptive_settings())
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
        guard(
            record=record,
            leverage=8,
            policy=adaptive_settings(**{"leverage.boost_enabled": False}),
        )


@pytest.mark.parametrize("field", ["tiers", "spread_fraction", "top_notional"])
def test_incomplete_eight_x_venue_facts_fail_with_fixed_safety_code(field):
    record = eight_x_record()
    record["snapshot"]["features"]["capital"]["leverage_decision"]["venue"].pop(field)
    with pytest.raises(
        ValueError,
        match="STRATEGY_CAPITAL_EVIDENCE_INVALID|CAPITAL_LEVERAGE_(SAFETY|LIQUIDITY)_UNVERIFIED",
    ):
        guard(record=record, leverage=8, policy=adaptive_settings())


@pytest.mark.parametrize(
    "field,value",
    [
        ("spread_fraction", "0.002"),
        ("top_notional", "749.999999999999999999"),
    ],
)
def test_eight_x_liquidity_budget_is_not_bypassed(field, value):
    record = eight_x_record()
    record["snapshot"]["features"]["capital"]["leverage_decision"]["venue"][field] = (
        value
    )
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_LIQUIDITY_UNVERIFIED"):
        guard(record=record, leverage=8, policy=adaptive_settings())


def test_eight_x_maintenance_tiers_and_margin_stress_must_cover_candidate():
    record = eight_x_record()
    record["snapshot"]["features"]["capital"]["leverage_decision"]["venue"]["tiers"][0][
        "cap"
    ] = "260"
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
        guard(record=record, leverage=8, policy=adaptive_settings())
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
        guard(
            record=eight_x_record(),
            leverage=8,
            policy=adaptive_settings(**{"leverage.max_margin_consumption": "0.1"}),
        )


def test_eight_x_margin_safety_includes_retained_original_risk_floor():
    with pytest.raises(ValueError, match="CAPITAL_LEVERAGE_SAFETY_UNVERIFIED"):
        guard(
            record=eight_x_record(),
            original="25",
            leverage=8,
            policy=adaptive_settings(**{"sizing.risk_fraction": "0.1"}),
        )


@pytest.mark.parametrize(
    "capital",
    [
        {"base": "NaN", "factor": "1"},
        {"base": "1000", "factor": "NaN"},
        {"base": "-1", "factor": "1"},
        {"base": "1000", "factor": "1.1"},
    ],
)
def test_invalid_new_capital_budget_is_rejected(capital):
    with pytest.raises(ValueError, match="INVALID_CAPITAL_RISK_BUDGET"):
        guard(capital=capital)


def test_factor_zero_halts_and_reduced_factor_applies_both_risk_limits():
    with pytest.raises(ValueError, match="CAPITAL_DRAWDOWN_HALT"):
        guard(capital={"base": "1000", "factor": "0"})
    with pytest.raises(ValueError, match="CAPITAL_SINGLE_TRADE_RISK_EXCEEDED"):
        guard(capital={"base": "1000", "factor": "0.5"})


def test_guard_and_mixed_held_budget_ignore_ambient_decimal_precision():
    _, held_record = material()
    kwargs = {
        "held_rows": [held(held_record), legacy_held()],
        "policy": settings(**{"sizing.cost_buffer_fraction": "0.01"}),
    }
    baseline, _ = guard(**kwargs)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result, _ = guard(**kwargs)
    assert result == baseline
    assert Decimal(result["proposed_stop_risk"]) == Decimal("17.525")
    assert Decimal(result["held_stop_risk"]) == Decimal("23.525")


def test_exact_derived_notional_beyond_storage_scale_survives_guard_low_precision():
    _, record = material(
        plan_changes={
            "quantity": "0.123456789012345678",
            "quantity_step": "0.000000000000000001",
            "entry_price": "100.123456789012345678",
            "stop_price": "95.123456789012345678",
            "planned_loss": "0.7",
        }
    )
    with localcontext() as context:
        context.prec = 100
        exact_notional = Decimal("0.123456789012345678") * Decimal(
            "100.123456789012345678"
        )
    assert exact_notional.as_tuple().exponent < -18
    baseline, _ = guard(record=record, notional=str(exact_notional))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        result, _ = guard(record=record, notional=str(exact_notional))
    assert result == baseline
    assert Decimal(result["proposed_stop_risk"]) >= Decimal("0.7")
