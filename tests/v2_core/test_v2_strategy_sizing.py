"""Pure typed sizing: real closed-bar fixture, exact money, no DB/venue calls."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from functools import lru_cache

import pytest

from tests.v2_core.test_v2_strategy_candidate_builder import bars, build
from v2_core.account_risk import AccountScope
from v2_core.adaptive_leverage import BOOST_CHECKS, safe_margin
from v2_core.evidence import canonical
from v2_core.strategy_capital import StrategyCapitalPlan
from v2_core.strategy_registry import DeploymentBinding, StrategySpec
from v2_core.strategy_sizing import (
    StrategySizingFacts,
    StrategySizingPolicy,
    size_candidate,
)


def sizing_policy(**changes):
    return StrategySizingPolicy(
        **{
            "pool_fraction": ".70",
            "single_margin_fraction": ".10",
            "single_risk_fraction": ".01",
            "cost_fraction": ".001",
            "price_drift_fraction": ".001",
            "stop_atr_multiple": "2",
            "structure_buffer_atr": ".25",
            "max_stop_fraction": ".12",
            "max_margin_consumption": ".80",
            "max_spread_fraction": ".001",
            "depth_multiple": "3",
            "allowed_leverages": (2, 3, 5),
            "boost_enabled": False,
            "max_fact_age_ms": 5000,
            "max_quote_age_ms": 1000,
            "entry_ttl_ms": 90000,
            **changes,
        }
    )


@lru_cache(maxsize=8)
def _decision(side, setup, scale):
    return build(bars(side=side, setup=setup, scale=scale))


def sizing_scene(
    *, side="BUY", setup="BREAKOUT", scale="1", policy=None, **fact_changes
):
    """Reusable pure fixture for root's separate isolated-PG bridge tests."""
    decision = _decision(side, setup, scale)
    policy = sizing_policy() if policy is None else policy
    scope = AccountScope("BINANCE", "qa-typed-sizing", "SANDBOX", "FUTURES")
    binding = DeploymentBinding(
        scope,
        "causal-instance",
        StrategySpec(
            "causal-trend",
            "candidate-v1",
            "strategy-trade-plan-v1",
            "causal-entry",
            "causal-exit",
        ),
        canonical(
            {
                "candidate_builder": decision.policy.binding_config(),
                "sizing": policy.binding_config(),
                "capital": policy.capital_config(),
            }
        ),
        1,
        "ACTIVE",
    )
    now = decision.decided_at_ms
    proof = {
        "selected": fact_changes.get("selected_leverage", 3),
        "checks": {key: False for key in sorted(BOOST_CHECKS)},
        "venue": {
            "symbol": "BTCUSDT",
            "observed_at_ms": now,
            "book_at_ms": now,
            "spread_fraction": ".0001",
            "top_notional": "1000000",
            "tiers": [
                {
                    "floor": "0",
                    "cap": "1000000000000",
                    "max_leverage": 20,
                    "mmr": ".005",
                }
            ],
        },
    }
    facts = StrategySizingFacts(
        **{
            "scope": scope,
            "symbol": "BTCUSDT",
            "observed_at_ms": now,
            "available_at_ms": now,
            "quote_at_ms": now,
            "account_at_ms": now,
            "filters_at_ms": now,
            "entry_price": decision.structure["values"]["close"],
            "mark_price": decision.structure["values"]["close"],
            "quantity_step": ".001",
            "min_quantity": ".001",
            "max_quantity": "1000000000000",
            "min_notional": "5",
            "max_notional": "1000000000000",
            "price_tick": str(Decimal(".01") * Decimal(scale)),
            "min_price": "0.000000000000000001",
            "max_price": "1000000000000",
            "capital_base": "1000",
            "available_margin": "1000",
            "used_margin": "0",
            "pending_reserved_margin": "0",
            "remaining_stop_risk": "100",
            "drawdown_factor": "1",
            "selected_leverage": 3,
            "leverage_proof_json": canonical(proof),
            **fact_changes,
        }
    )
    return decision, binding, policy, facts


def run_size(scene=None, **changes):
    decision, binding, policy, facts = sizing_scene() if scene is None else scene
    return size_candidate(
        decision,
        **{
            "binding": binding,
            "policy": policy,
            "facts": facts,
            "expires_at_ms": decision.decided_at_ms + policy.entry_ttl_ms,
            "exit_version": "causal-exit-v1",
            "exit_config": {"mode": "FIXED_STOP"},
            **changes,
        },
    )


def _proof(facts, **venue_changes):
    proof = facts.leverage_proof
    proof["venue"].update(venue_changes)
    return replace(facts, leverage_proof_json=canonical(proof))


def _q(result):
    assert result.status == "SIZED", result.reason
    return Fraction(result.plan.quantity)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("setup", ["BREAKOUT", "PULLBACK_RESUME"])
def test_real_causal_structures_size_without_legacy_identity_or_score(side, setup):
    scene = sizing_scene(side=side, setup=setup)
    decision, binding, policy, facts = scene
    result = run_size(scene)
    plan = result.plan
    assert result.status == "SIZED"
    assert plan.side == side
    assert plan.opportunity_id == decision.opportunity_id
    assert plan.scope == facts.scope == binding.scope
    assert plan.config_digest == binding.config_digest
    assert plan.margin_mode == "ISOLATED"
    assert plan.exit_handler == binding.spec.exit_handler
    assert not result.execution_authorized and not plan.execution_authorized
    assert "score" not in result.evidence["calculation"]
    assert result.capital_evidence["leverage_decision"] == facts.leverage_proof
    capital = StrategyCapitalPlan(
        plan,
        Decimal(policy.cost_fraction),
        Decimal(policy.price_drift_fraction),
        canonical(facts.leverage_proof),
    )
    with localcontext() as context:
        context.prec = 100
        notional = Decimal(plan.entry_price) * Decimal(plan.quantity)
        assert capital.stop_risk(
            str(notional), current_cost_fraction=policy.cost_fraction
        ) == Decimal(plan.planned_loss)


@pytest.mark.parametrize("leverage", [2, 3, 5])
def test_explicit_low_tiers_do_not_require_or_fabricate_model_score(leverage):
    scene = sizing_scene(selected_leverage=leverage)
    result = run_size(scene)
    assert result.plan.leverage == leverage
    assert not any(result.capital_evidence["leverage_decision"]["checks"].values())


def test_seventy_percent_is_maximum_total_margin_not_idle_reserve():
    policy = sizing_policy(single_margin_fraction=".70", single_risk_fraction="1")
    scene = sizing_scene(policy=policy, used_margin="600", remaining_stop_risk="1000")
    result = run_size(scene)
    q = _q(result)
    high = Fraction(result.evidence["calculation"]["price_high"])
    assert result.evidence["calculation"]["pool_remaining"] == "100"
    assert Fraction(600) + q * high / 3 <= 700
    assert Fraction(600) + (q + Fraction(".001")) * high / 3 > 700
    full_pool = run_size(sizing_scene(policy=policy, remaining_stop_risk="1000"))
    assert Fraction(full_pool.evidence["calculation"]["reserved_margin"]) > 600
    assert Fraction(full_pool.evidence["calculation"]["reserved_margin"]) <= 700


def test_pending_margin_reduces_both_pool_and_cash_and_never_overcommits():
    policy = sizing_policy(single_margin_fraction=".70", single_risk_fraction="1")
    result = run_size(
        sizing_scene(
            policy=policy,
            used_margin="600",
            pending_reserved_margin="90",
            available_margin="100",
            remaining_stop_risk="1000",
        )
    )
    assert _q(result) > 0
    assert result.evidence["calculation"]["pool_remaining"] == "10"
    assert result.evidence["calculation"]["available_after_pending"] == "10"
    q, high = (
        Fraction(result.plan.quantity),
        Fraction(result.evidence["calculation"]["price_high"]),
    )
    assert q * high * (Fraction(1, 3) + Fraction(".001")) <= 10
    assert 600 + 90 + q * high / 3 <= 700


@pytest.mark.parametrize(
    "changes",
    [
        {"capital_base": "0"},
        {"available_margin": "0"},
        {"used_margin": "700"},
        {"used_margin": "800"},
        {"pending_reserved_margin": "1000"},
        {"remaining_stop_risk": "0"},
        {"drawdown_factor": "0"},
    ],
)
def test_zero_or_exhausted_budget_is_nonterminal_no_plan(changes):
    result = run_size(sizing_scene(**changes))
    assert result.status == "NO_BUDGET"
    assert result.reason == "NO_USABLE_BUDGET"
    assert result.plan is None and not result.execution_authorized


def test_portfolio_residual_is_not_drawdown_discounted_twice():
    scene = sizing_scene(
        drawdown_factor=".5", remaining_stop_risk=".1", min_notional=".01"
    )
    result = run_size(scene)
    assert _q(result) > 0
    loss = Fraction(result.plan.planned_loss)
    assert Fraction(".05") < loss <= Fraction(".1")
    assert result.evidence["calculation"]["stop_risk_limit"] == "0.1"


@pytest.mark.parametrize("factor", [".9", ".5", ".1"])
def test_drawdown_factor_reduces_single_trade_margin_and_stop_risk(factor):
    result = run_size(sizing_scene(drawdown_factor=factor))
    assert _q(result) < _q(run_size())
    assert Fraction(result.plan.planned_loss) <= 1000 * Fraction(".01") * Fraction(
        factor
    )
    assert Fraction(
        result.evidence["calculation"]["reserved_margin"]
    ) <= 1000 * Fraction(".1") * Fraction(factor)


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_quantity", "100"),
        ("min_notional", "1000"),
        ("quantity_step", "100"),
    ],
)
def test_never_rounds_up_to_meet_venue_minimum(field, value):
    result = run_size(sizing_scene(**{field: value}))
    assert result.status == "NO_BUDGET" and result.plan is None
    assert result.reason in {"BELOW_VENUE_MINIMUM", "NO_USABLE_BUDGET"}


@pytest.mark.parametrize(
    "field,value", [("max_quantity", ".02"), ("max_notional", "8")]
)
def test_quantity_and_notional_venue_upper_limits_apply_to_envelope(field, value):
    result = run_size(sizing_scene(min_notional=".1", **{field: value}))
    q = _q(result)
    assert (
        q <= Fraction(".02")
        if field == "max_quantity"
        else q * Fraction(result.evidence["calculation"]["price_high"]) <= 8
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_structural_stop_tick_rounds_outward_and_never_shrinks_to_fit_leverage(side):
    result = run_size(sizing_scene(side=side, price_tick=".1"))
    assert _q(result) > 0
    summary = result.evidence["calculation"]
    raw, rounded = Fraction(summary["raw_stop"]), Fraction(result.plan.stop_price)
    assert rounded <= raw if side == "BUY" else rounded >= raw
    assert (rounded / Fraction(".1")).denominator == 1
    strict = sizing_policy(max_stop_fraction=".0001")
    refused = run_size(sizing_scene(side=side, policy=strict))
    assert refused.status == "NO_BUDGET" and refused.reason == "STOP_DISTANCE_LIMIT"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_risk_includes_frozen_stop_cost_and_drift_not_just_distance(side):
    base = sizing_policy(single_margin_fraction=".7", single_risk_fraction=".001")
    first = run_size(sizing_scene(side=side, policy=base))
    costly = run_size(
        sizing_scene(side=side, policy=replace(base, cost_fraction=".01"))
    )
    wider = run_size(
        sizing_scene(side=side, policy=replace(base, price_drift_fraction=".002"))
    )
    assert _q(costly) < _q(first)
    assert _q(wider) < _q(first)
    for result in (first, costly, wider):
        q = Fraction(result.plan.quantity)
        raw = q * abs(
            Fraction(result.plan.entry_price) - Fraction(result.plan.stop_price)
        )
        assert raw < Fraction(result.plan.planned_loss) <= 1
    assert first.plan.stop_price == costly.plan.stop_price == wider.plan.stop_price


def test_tiny_residual_rounds_loss_up_but_budget_down_without_one_quantum_overrun():
    result = run_size(
        sizing_scene(
            remaining_stop_risk="0.000000000000000019",
            quantity_step="0.000000000000000001",
            min_quantity="0.000000000000000001",
            min_notional="0.000000000000000001",
        )
    )
    assert _q(result) > 0
    assert Decimal(result.plan.planned_loss) <= Decimal("0.000000000000000019")
    assert Fraction(result.plan.planned_loss) >= Fraction(
        result.plan.quantity
    ) * Fraction(result.evidence["calculation"]["unit_stop_risk"])


def test_hostile_ambient_decimal_context_cannot_change_risk_or_quantity():
    scene = sizing_scene(
        entry_price="108.900000000000000001", quantity_step="0.000000000000000001"
    )
    expected = run_size(scene)
    assert _q(expected) > 0
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        actual = run_size(scene)
    assert actual == expected


def test_low_price_contract_uses_exact_tick_and_step_without_float_epsilon():
    result = run_size(sizing_scene(scale="0.000000001", quantity_step=".000001"))
    assert _q(result) > 0
    assert (Fraction(result.plan.quantity) / Fraction(".000001")).denominator == 1
    assert (
        Fraction(result.plan.stop_price) / Fraction(".00000000001")
    ).denominator == 1
    assert len(result.plan.planned_loss.split(".")[-1]) <= 18


@pytest.mark.parametrize(
    "field,value",
    [
        ("pool_fraction", ".700000000000000001"),
        ("pool_fraction", "0"),
        ("single_margin_fraction", ".8"),
        ("single_risk_fraction", "0"),
        ("cost_fraction", "0"),
        ("price_drift_fraction", "0"),
        ("max_margin_consumption", "1.01"),
        ("depth_multiple", ".9"),
        ("structure_buffer_atr", "-.1"),
        ("stop_atr_multiple", "0"),
        ("allowed_leverages", (3, 2)),
        ("allowed_leverages", (2, 2, 3)),
        ("allowed_leverages", (2, True)),
        ("allowed_leverages", [2, 3]),
        ("allowed_leverages", (1, 2, 3)),
        ("boost_enabled", 1),
        ("entry_ttl_ms", 0),
        ("max_fact_age_ms", True),
    ],
)
def test_unsafe_or_implicit_policy_is_rejected(field, value):
    with pytest.raises((ValueError, TypeError)):
        sizing_policy(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("capital_base", "-1"),
        ("available_margin", "-1"),
        ("used_margin", "-1"),
        ("pending_reserved_margin", "-1"),
        ("remaining_stop_risk", "-1"),
        ("drawdown_factor", "1.1"),
        ("drawdown_factor", None),
        ("entry_price", "0"),
        ("mark_price", "NaN"),
        ("quantity_step", ".0000000000000000001"),
        ("price_tick", 0.01),
        ("min_quantity", "1000000000001"),
        ("min_notional", "1000000000001"),
        ("selected_leverage", True),
        ("selected_leverage", 4),
        ("leverage_proof_json", "{}"),
    ],
)
def test_bad_facts_never_yield_a_plan(field, value):
    with pytest.raises((ValueError, TypeError)):
        run_size(sizing_scene(**{field: value}))


@pytest.mark.parametrize(
    "field,age",
    [("account_at_ms", 5001), ("filters_at_ms", 5001), ("quote_at_ms", 1001)],
)
def test_each_fact_stream_has_its_own_freshness_limit(field, age):
    decision, binding, policy, facts = sizing_scene()
    facts = replace(facts, **{field: decision.decided_at_ms - age})
    with pytest.raises(ValueError, match="SIZING_STALE_FACTS"):
        run_size((decision, binding, policy, facts))


def test_future_availability_and_noncausal_collection_are_rejected():
    decision, binding, policy, facts = sizing_scene()
    with pytest.raises(ValueError, match="SIZING_FUTURE_FACTS"):
        run_size(
            (
                decision,
                binding,
                policy,
                replace(facts, available_at_ms=decision.decided_at_ms + 1),
            )
        )
    with pytest.raises(ValueError, match="SIZING_NONCAUSAL_FACTS"):
        replace(facts, quote_at_ms=facts.observed_at_ms + 1)


@pytest.mark.parametrize("delta", [0, -1, 90001, 86400001])
def test_expired_or_extended_plan_lifetime_is_rejected(delta):
    decision, *_ = sizing_scene()
    with pytest.raises(ValueError, match="SIZING_PLAN_LIFETIME"):
        run_size(expires_at_ms=decision.decided_at_ms + delta)


@pytest.mark.parametrize("field,value", [("entry_price", "120"), ("mark_price", "120")])
def test_quote_divergence_does_not_reuse_old_structural_stop(field, value):
    result = run_size(sizing_scene(**{field: value}))
    assert result.status == "NO_BUDGET" and result.reason == "REFERENCE_PRICE_DRIFT"


@pytest.mark.parametrize("mode", ["OFF", "DRAINING"])
def test_disabled_deployment_is_observation_not_terminal_or_execution(mode):
    decision, binding, policy, facts = sizing_scene()
    result = run_size((decision, replace(binding, mode=mode), policy, facts))
    assert result.status == "NO_BUDGET" and result.reason == "DEPLOYMENT_NOT_EVALUABLE"


def test_shadow_is_sizable_but_never_permission():
    decision, binding, policy, facts = sizing_scene()
    result = run_size((decision, replace(binding, mode="SHADOW"), policy, facts))
    assert result.status == "SIZED" and not result.execution_authorized


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "environment",
        "symbol",
        "capital_config",
        "sizing_policy",
        "candidate_policy",
    ],
)
def test_scope_or_frozen_config_mismatch_never_sizes(change):
    decision, binding, policy, facts = sizing_scene()
    if change == "account":
        facts = replace(facts, scope=replace(facts.scope, account_id="other"))
    elif change == "environment":
        facts = replace(facts, scope=replace(facts.scope, environment="LIVE"))
    elif change == "symbol":
        facts = replace(facts, symbol="ETHUSDT")
    else:
        config = binding.config
        config.pop(
            {
                "capital_config": "capital",
                "sizing_policy": "sizing",
                "candidate_policy": "candidate_builder",
            }[change]
        )
        binding = replace(binding, config_json=canonical(config))
    with pytest.raises(
        ValueError, match="SIZING_(SCOPE_OR_SCHEMA|FROZEN_POLICY)_MISMATCH"
    ):
        run_size((decision, binding, policy, facts))


def _boost_scene():
    scene = sizing_scene(
        policy=sizing_policy(allowed_leverages=(2, 3, 5, 8), boost_enabled=True),
        selected_leverage=8,
    )
    decision, binding, policy, facts = scene
    proof = facts.leverage_proof
    proof["checks"] = {key: True for key in BOOST_CHECKS}
    return (
        decision,
        binding,
        policy,
        replace(facts, leverage_proof_json=canonical(proof)),
    )


def test_eight_requires_explicit_policy_and_provider_evidence_without_changing_stop():
    boost = run_size(_boost_scene())
    assert _q(boost) > 0 and boost.plan.leverage == 8
    assert boost.plan.stop_price == run_size().plan.stop_price
    decision, binding, policy, facts = _boost_scene()
    disabled = replace(policy, boost_enabled=False)
    config = binding.config
    config["sizing"] = disabled.binding_config()
    with pytest.raises(ValueError, match="SIZING_BOOST_UNVERIFIED"):
        run_size(
            (decision, replace(binding, config_json=canonical(config)), disabled, facts)
        )


@pytest.mark.parametrize("check", sorted(BOOST_CHECKS))
@pytest.mark.parametrize("false_value", [False, None, 1])
def test_every_original_boost_check_is_required_and_never_invented(check, false_value):
    decision, binding, policy, facts = _boost_scene()
    proof = facts.leverage_proof
    proof["checks"][check] = false_value
    with pytest.raises(ValueError, match="SIZING_BOOST_UNVERIFIED"):
        run_size(
            (
                decision,
                binding,
                policy,
                replace(facts, leverage_proof_json=canonical(proof)),
            )
        )


@pytest.mark.parametrize(
    "change",
    [
        "missing_venue",
        "other_symbol",
        "old_venue",
        "future_venue",
        "bad_tiers",
        "bad_bool",
        "wrong_selected",
    ],
)
def test_leverage_proof_integrity_checked_even_for_two_x(change):
    decision, binding, policy, facts = sizing_scene(selected_leverage=2)
    proof = facts.leverage_proof
    if change == "missing_venue":
        proof.pop("venue")
    elif change == "other_symbol":
        proof["venue"]["symbol"] = "ETHUSDT"
    elif change == "old_venue":
        proof["venue"]["observed_at_ms"] -= 1
    elif change == "future_venue":
        proof["venue"]["book_at_ms"] += 1
    elif change == "bad_tiers":
        proof["venue"]["tiers"][0]["floor"] = "1"
    elif change == "bad_bool":
        proof["checks"]["flow"] = 1
    else:
        proof["selected"] = 3
    with pytest.raises(ValueError):
        run_size(
            (
                decision,
                binding,
                policy,
                replace(facts, leverage_proof_json=canonical(proof)),
            )
        )


@pytest.mark.parametrize(
    "venue,reason",
    [
        ({"spread_fraction": ".002"}, "LEVERAGE_LIQUIDITY_LIMIT"),
        ({"top_notional": "1"}, "LEVERAGE_LIQUIDITY_LIMIT"),
        (
            {
                "tiers": [
                    {"floor": "0", "cap": "200", "max_leverage": 20, "mmr": ".005"}
                ]
            },
            "LEVERAGE_MARGIN_UNSAFE",
        ),
        (
            {
                "tiers": [
                    {"floor": "0", "cap": "1000000", "max_leverage": 2, "mmr": ".005"}
                ]
            },
            "LEVERAGE_MARGIN_UNSAFE",
        ),
        (
            {
                "tiers": [
                    {"floor": "0", "cap": "1000000", "max_leverage": 20, "mmr": ".3"}
                ]
            },
            "LEVERAGE_MARGIN_UNSAFE",
        ),
    ],
)
def test_liquidity_and_maintenance_failures_reject_without_tightening_stop(
    venue, reason
):
    decision, binding, policy, facts = sizing_scene()
    result = run_size((decision, binding, policy, _proof(facts, **venue)))
    assert result.status == "NO_BUDGET" and result.reason == reason
    assert result.evidence["calculation"]["stop_price"] == run_size().plan.stop_price


def test_zero_spread_is_observed_fact_but_missing_spread_is_invalid():
    decision, binding, policy, facts = sizing_scene()
    assert (
        run_size((decision, binding, policy, _proof(facts, spread_fraction="0"))).status
        == "SIZED"
    )
    proof = facts.leverage_proof
    proof["venue"].pop("spread_fraction")
    with pytest.raises(ValueError):
        run_size(
            (
                decision,
                binding,
                policy,
                replace(facts, leverage_proof_json=canonical(proof)),
            )
        )


def test_result_and_facts_do_not_expose_mutable_nested_state():
    scene = sizing_scene()
    result = run_size(scene)
    evidence = result.evidence
    evidence["facts"]["capital_base"] = "999999"
    result.capital_evidence["leverage_decision"]["selected"] = 8
    assert result.evidence["facts"]["capital_base"] == "1000"
    assert result.capital_evidence["leverage_decision"]["selected"] == 3
    with pytest.raises(FrozenInstanceError):
        result.status = "SIZED"
    assert run_size(scene) == result


def test_existing_safe_margin_agrees_for_normal_sized_reference():
    decision, binding, policy, facts = sizing_scene()
    result = run_size((decision, binding, policy, facts))
    plan = result.plan
    with localcontext() as context:
        context.prec = 100
        notional = Decimal(plan.quantity) * Decimal(plan.entry_price)
        risk_stop = Decimal(plan.planned_loss) / notional - Decimal(
            policy.cost_fraction
        )
        assert safe_margin(
            plan.leverage,
            risk_stop,
            notional,
            facts.leverage_proof["venue"],
            {
                "sizing.cost_buffer_fraction": policy.cost_fraction,
                "leverage.max_margin_consumption": policy.max_margin_consumption,
            },
        )


def test_policy_json_roundtrip_and_explicit_capital_fragment():
    policy = sizing_policy()
    config = json.loads(canonical(policy.binding_config()))
    values = config["policy"]
    values["allowed_leverages"] = tuple(values["allowed_leverages"])
    assert StrategySizingPolicy(**values) == policy
    assert policy.capital_config() == {
        "schema": "strategy-capital-v1",
        "roundtrip_cost_fraction": "0.001",
        "price_drift_fraction": "0.001",
    }


def test_fractional_pool_and_margin_caps_round_down_before_reserve_rounds_up():
    policy = sizing_policy(single_margin_fraction=".7", single_risk_fraction="1")
    scene = sizing_scene(
        policy=policy,
        capital_base="1000.000000000000000001",
        used_margin="699.999999999999999990",
        quantity_step="0.000000000000000001",
        min_quantity="0.000000000000000001",
        min_notional="0.000000000000000001",
    )
    result = run_size(scene)
    assert result.reason == "NO_USABLE_BUDGET"
    assert result.evidence["calculation"]["pool_remaining"] == "0.00000000000000001"
    bigger = run_size(
        sizing_scene(
            policy=policy,
            capital_base="1000.000000000000000001",
            used_margin="699.999999999999990000",
            quantity_step="0.000000000000000001",
            min_quantity="0.000000000000000001",
            min_notional="0.000000000000000001",
        )
    )
    assert _q(bigger) > 0
    assert Fraction("699.999999999999990000") + Fraction(
        bigger.evidence["calculation"]["reserved_margin"]
    ) <= Fraction("1000.000000000000000001") * Fraction(".7")


def test_unknown_and_no_setup_remain_observations_without_fake_trade_plan():
    decision, binding, policy, facts = sizing_scene()
    rejected = replace(
        decision, policy=replace(decision.policy, min_volume_ratio="100")
    )
    config = binding.config
    config["candidate_builder"] = rejected.policy.binding_config()
    result = run_size(
        (rejected, replace(binding, config_json=canonical(config)), policy, facts)
    )
    assert (
        result.status == "NO_BUDGET"
        and result.reason == "NO_SETUP"
        and result.plan is None
    )
    warmup = build(bars()[:20])
    now = warmup.decided_at_ms
    config["candidate_builder"] = warmup.policy.binding_config()
    facts = replace(
        facts,
        observed_at_ms=now,
        available_at_ms=now,
        quote_at_ms=now,
        account_at_ms=now,
        filters_at_ms=now,
    )
    result = run_size(
        (warmup, replace(binding, config_json=canonical(config)), policy, facts)
    )
    assert (
        result.status == "NO_BUDGET"
        and result.reason == "UNKNOWN"
        and result.plan is None
    )


def test_plan_quantity_changes_do_not_create_a_new_structural_opportunity():
    first = run_size(sizing_scene())
    reduced = run_size(sizing_scene(capital_base="500"))
    assert _q(first) > _q(reduced)
    assert first.plan.opportunity_id == reduced.plan.opportunity_id
    assert first.plan.plan_digest != reduced.plan.plan_digest


@pytest.mark.parametrize("checks", [None, [], "yes"])
def test_malformed_boost_container_has_bounded_failure_not_attribute_error(checks):
    decision, binding, policy, facts = _boost_scene()
    proof = facts.leverage_proof
    proof["checks"] = checks
    with pytest.raises(ValueError, match="SIZING_LEVERAGE_EVIDENCE_REQUIRED"):
        run_size(
            (
                decision,
                binding,
                policy,
                replace(facts, leverage_proof_json=canonical(proof)),
            )
        )


def test_duplicate_or_oversize_proof_json_is_rejected_before_interpretation():
    *_, facts = sizing_scene()
    with pytest.raises(ValueError, match="SIZING_DUPLICATE_JSON_FIELD"):
        replace(facts, leverage_proof_json='{"selected":2,"selected":8}')
    with pytest.raises(ValueError, match="SIZING_JSON_BOUND"):
        replace(facts, leverage_proof_json=canonical({"padding": "x" * 65536}))
