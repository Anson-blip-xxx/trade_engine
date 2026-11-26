"""Pure causal candidate research contracts; no database, credentials or venue."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal, Inexact, localcontext
from uuid import uuid4

import pytest

from v2_core import entry_controls, liquidity_controls
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.strategy_candidate_builder import (
    BUILDER_VERSION,
    CandidatePolicy,
    StructureCandidateDecision,
    assemble_frozen_candidate,
    build_candidate,
)
from v2_core.strategy_entry_worker import EntryReceipt, EntrySignalState
from v2_core.strategy_execution_context import execution_context
from v2_core.strategy_plan import PLAN_SCHEMA, TradePlan
from v2_core.strategy_registry import DeploymentBinding, StrategySpec
from v2_core.trend_structure import build_trend_structure

MINUTE = 60000
WIDTH = 15 * MINUTE


def policy(**changes):
    return CandidatePolicy(
        **{
            "min_volume_ratio": "0.8",
            "min_directional_taker_ratio": "0.55",
            "min_ema_slope_atr": "0",
            "min_breakout_atr": "0",
            "max_extension_atr": "20",
            "max_atr_pct": "10",
            "max_structure_age_ms": WIDTH,
            "max_decision_delay_ms": 60000,
            **changes,
        }
    )


def bars(*, side="BUY", setup="BREAKOUT", scale="1"):
    rows = []
    scale = Decimal(scale)
    for minute in range(1350):
        closed_bar = minute // 15
        price = Decimal(100) + Decimal(closed_bar) / 10
        low, high = price - Decimal("0.03"), price + Decimal("0.03")
        if setup == "PULLBACK_RESUME":
            if closed_bar == 87:
                price, low, high = Decimal(108), Decimal("107.3"), Decimal("108.03")
            elif closed_bar == 88:
                price, low, high = (
                    Decimal("108.1"),
                    Decimal("108.07"),
                    Decimal("108.13"),
                )
            elif closed_bar == 89:
                price, low, high = (
                    Decimal("108.4"),
                    Decimal("108.37"),
                    Decimal("108.43"),
                )
        opened = price - Decimal("0.01")
        buy = Decimal(7)
        if side == "SELL":
            opened, price, low, high = 300 - opened, 300 - price, 300 - high, 300 - low
            buy = 10 - buy
        rows.append(
            {
                "t": minute * MINUTE,
                "o": str(opened * scale),
                "h": str(high * scale),
                "l": str(low * scale),
                "c": str(price * scale),
                "v": "10",
                "tbv": str(buy),
            }
        )
    return rows


def build(rows=None, **changes):
    rows = bars() if rows is None else rows
    closed = rows[-1]["t"] + MINUTE
    return build_candidate(
        rows,
        **{
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "closed_at_ms": closed,
            "available_at_ms": closed,
            "decided_at_ms": closed,
            "policy": policy(),
            **changes,
        },
    )


def safety(plan):
    def controls(module):
        values = module.resolve({})
        return {
            "version": 0,
            "values": values,
            "digest": digest(
                canonical({"scope": asdict(plan.scope), "version": 0, "values": values})
            ),
        }

    return execution_context(
        plan,
        entry_controls=controls(entry_controls),
        liquidity_controls=controls(liquidity_controls),
        policy={
            "entry.reentry_enabled": False,
            "entry.price_guard_enabled": False,
            "entry.max_adverse_price_fraction": "0.01",
            "entry.max_stop_risk_increase": "1.10",
        },
    )


def scene(
    decision=None,
    *,
    source="tv_bridge",
    account="qa-builder",
    strategy="research-trend",
    version="candidate-v1",
):
    decision = build() if decision is None else decision
    structure = decision.structure
    scope = AccountScope("BINANCE", account, structure["environment"], "FUTURES")
    binding = DeploymentBinding(
        scope,
        "research-instance",
        StrategySpec(
            strategy, version, PLAN_SCHEMA, "closed-structure", "research-exit"
        ),
        canonical({"candidate_builder": decision.policy.binding_config()}),
        1,
        "ACTIVE",
    )
    entry = Decimal(structure["values"]["close"])
    stop = entry - 1 if decision.side == "BUY" else entry + 1
    plan = TradePlan(
        scope=scope,
        instance_id=binding.instance_id,
        strategy_id=strategy,
        code_version=version,
        deployment_generation=1,
        config_digest=binding.config_digest,
        opportunity_id=decision.opportunity_id,
        symbol=structure["symbol"],
        side=decision.side,
        quantity="0.125",
        entry_price=str(entry),
        stop_price=str(stop),
        planned_loss="0.15",
        quantity_step="0.001",
        leverage=3,
        margin_mode="ISOLATED",
        observed_at_ms=decision.observed_at_ms,
        decided_at_ms=decision.decided_at_ms,
        expires_at_ms=decision.decided_at_ms + 60000,
        exit_handler=binding.spec.exit_handler,
        exit_version="exit-v1",
        exit_config_json='{"research_only":true}',
    )
    signal = EntrySignalState(
        str(uuid4()),
        source,
        scope.environment,
        canonical(
            {
                "observed_at": decision.observed_at_ms,
                "expires_at_ms": plan.expires_at_ms,
                "symbol": plan.symbol,
                # Deliberately contradict the server direction for BUY. This
                # hint requests server re-evaluation; it is not trade authority.
                "signal": "TREND_DOWN_SHORT",
                "features": {"strength": 99, "setup": "PUMP", "timeframe": "1"},
            }
        ),
        decision.decided_at_ms,
    )
    return decision, signal, binding, plan, safety(plan)


def assemble(parts, **changes):
    decision, signal, binding, plan, execution = parts
    return assemble_frozen_candidate(
        decision,
        **{
            "signal": signal,
            "binding": binding,
            "plan": plan,
            "execution": execution,
            **changes,
        },
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("setup", ["BREAKOUT", "PULLBACK_RESUME"])
def test_server_structure_generates_symmetric_explicit_research_candidates(side, setup):
    decision = build(bars(side=side, setup=setup))
    assert (decision.status, decision.side, decision.setup) == (
        "CANDIDATE",
        side,
        setup,
    )
    assert decision.execution_authorized is False
    assert decision.opportunity_id.startswith("structure15m:")
    candidate = assemble(scene(decision))
    assert candidate.action == "OPEN" and candidate.plan.side == side
    assert candidate.execution_authorized is False
    assert candidate.features["candidate_builder"] == decision.payload()


def test_reuses_existing_indicator_contract_without_reimplementing_indicators():
    rows = bars()
    result = build(rows)
    assert result.structure == build_trend_structure(
        rows, symbol="BTCUSDT", environment="SANDBOX", closed_at_ms=1350 * MINUTE
    )
    assert result.structure["contract"]["prior_windows_exclude_current"] is True
    assert "score" not in canonical(result.payload()).lower()


@pytest.mark.parametrize("source", ["tv_bridge", "s3", "s2", "quant"])
def test_scan_sources_work_without_tv_and_identity_ignores_signal_hints(source):
    parts = scene(source=source)
    candidate = assemble(parts)
    state = parts[1]
    snapshot = state.snapshot
    snapshot["signal"] = "VIOLENT_BULLISH"
    snapshot["features"] = {"strength": 0, "setup": "RANDOM", "timeframe": "240"}
    changed = assemble(parts, signal=replace(state, snapshot_json=canonical(snapshot)))
    assert candidate.plan.opportunity_id == changed.plan.opportunity_id
    assert candidate.plan.side == changed.plan.side == "BUY"
    assert candidate.signal_digest != changed.signal_digest
    assert (
        candidate.features["candidate_builder"] == changed.features["candidate_builder"]
    )


def test_opportunity_stable_across_sources_accounts_strategies_versions_and_policies():
    decision = build()
    original = assemble(scene(decision))
    changed = assemble(
        scene(
            decision,
            source="s3",
            account="qa-other",
            strategy="other-trend",
            version="candidate-v2",
        )
    )
    revised = build(policy=policy(min_volume_ratio="0.7"))
    assert (
        original.plan.opportunity_id
        == changed.plan.opportunity_id
        == revised.opportunity_id
    )
    assert original.candidate_digest != changed.candidate_digest
    assert original.plan.plan_digest != changed.plan.plan_digest
    assert revised.decision_digest != decision.decision_digest


def test_new_closed_minute_summary_and_cache_rotation_do_not_create_new_opportunity():
    baseline = bars()
    prefix = [{**baseline[0], "t": index * MINUTE} for index in range(15)]
    rows = prefix + [{**row, "t": row["t"] + 15 * MINUTE} for row in baseline]
    original = build(rows)
    for _ in range(1, 15):
        next_row = {
            **rows[-1],
            "t": rows[-1]["t"] + MINUTE,
            "o": "10000",
            "c": "10000",
            "h": "10001",
            "l": "9999",
            "v": "1000",
            "tbv": "1000",
        }
        rows.append(next_row)
        # Keep one extra complete 15m bar so all 90 indicator bars survive
        # rotation during the partial new 15m bucket.
        rows.pop(0)
        current = build(rows)
        assert current.structure["values"] == original.structure["values"]
        assert current.opportunity_id == original.opportunity_id
        assert current.structure["input_digest"] != original.structure["input_digest"]
    assert current.observed_at_ms > original.observed_at_ms
    assert (
        current.structure["last_closed_at_ms"]
        == original.structure["last_closed_at_ms"]
    )


def test_extra_old_history_does_not_change_opportunity_or_indicator_seed():
    rows = bars()
    prefix = [{**rows[0], "t": index * MINUTE} for index in range(15)]
    shifted = [{**row, "t": row["t"] + 15 * MINUTE} for row in rows]
    short, full = build(shifted), build(prefix + shifted)
    assert short.structure["values"] == full.structure["values"]
    assert short.opportunity_id == full.opportunity_id
    assert short.structure["input_digest"] != full.structure["input_digest"]


def test_environment_symbol_direction_and_new_closed_bar_partition_identity():
    original = build()
    shifted = [{**row, "t": row["t"] + WIDTH} for row in bars()]
    variants = [
        build(environment="LIVE"),
        build(symbol="ETHUSDT"),
        build(bars(side="SELL")),
        build(shifted),
    ]
    assert (
        len({original.opportunity_id, *(row.opportunity_id for row in variants)}) == 5
    )


@pytest.mark.parametrize("count", [1, 14, 15, 60, 1335, 1349])
def test_warmup_is_unknown_not_open_or_negative_terminal(count):
    decision = build(bars()[:count])
    assert (decision.status, decision.reason) == ("UNKNOWN", "STRUCTURE_WARMUP")
    assert decision.side is decision.setup is decision.opportunity_id is None
    parts = scene()
    with pytest.raises(ValueError, match="CANDIDATE_SETUP_REQUIRED"):
        assemble((decision, *parts[1:]))


@pytest.mark.parametrize("side,expected", [("BUY", "NO_SETUP"), ("SELL", "CANDIDATE")])
def test_zero_taker_buy_is_observed_sell_flow_not_missing(side, expected):
    rows = bars(side=side)
    for row in rows[-15:]:
        row["tbv"] = "0"
    result = build(rows)
    assert result.structure["values"]["taker_buy_ratio"] == "0"
    assert result.status == expected
    if side == "BUY":
        assert result.reason == "FLOW_UNCONFIRMED"


@pytest.mark.parametrize(
    "which,reason",
    [
        ("all", "TAKER_FLOW_UNAVAILABLE"),
        ("current", "TAKER_FLOW_UNAVAILABLE"),
        ("prior", "VOLUME_BASELINE_UNAVAILABLE"),
    ],
)
def test_zero_volume_and_unknown_baseline_are_distinct_unknowns(which, reason):
    rows = bars()
    affected = (
        rows if which == "all" else rows[-15:] if which == "current" else rows[:-15]
    )
    for row in affected:
        row.update(v="0", tbv="0")
    result = build(rows)
    assert (result.status, result.reason) == ("UNKNOWN", reason)
    assert result.opportunity_id is None
    if which == "current":
        assert result.structure["values"]["volume_ratio"] == "0"


def test_flat_zero_atr_does_not_manufacture_a_trend_or_divide_by_zero():
    rows = bars()
    for row in rows:
        row.update(o="100", h="100", l="100", c="100")
    result = build(rows)
    assert (result.status, result.reason) == ("UNKNOWN", "ZERO_ATR")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"min_volume_ratio": "2"}, "VOLUME_UNCONFIRMED"),
        ({"min_directional_taker_ratio": "0.8"}, "FLOW_UNCONFIRMED"),
        ({"min_ema_slope_atr": "10"}, "STRUCTURE_UNCONFIRMED"),
        ({"max_extension_atr": "0.01"}, "EXTENSION_LIMIT"),
        ({"max_atr_pct": "0.001"}, "VOLATILITY_LIMIT"),
    ],
)
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_explicit_thresholds_are_symmetric_and_do_not_claim_probability(
    changes, reason, side
):
    result = build(bars(side=side), policy=policy(**changes))
    assert (result.status, result.reason) == ("NO_SETUP", reason)
    assert result.side is result.opportunity_id is None


def test_breakout_buffer_is_explicit_and_cannot_be_satisfied_by_strong_signal():
    result = build(policy=policy(min_breakout_atr="100"))
    assert (result.status, result.reason) == ("NO_SETUP", "BREAKOUT_BUFFER_UNCONFIRMED")
    assert result.opportunity_id is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_failed_breakout_cannot_bypass_buffer_by_reclassifying_as_pullback(side):
    rows = bars(side=side)
    # Add a real prior-bar pullback touch without changing its close or the
    # subsequent closed-bar breakout. Both shapes now share the same bar.
    for row in rows[-30:-15]:
        row["l" if side == "BUY" else "h"] = "106" if side == "BUY" else "194"
    ordinary = build(rows)
    assert ordinary.setup == "BREAKOUT"
    assert (
        ordinary.structure["values"][
            f"prior3_touched_ema20_{'long' if side == 'BUY' else 'short'}"
        ]
        is True
    )
    strict = build(rows, policy=policy(min_breakout_atr="100"))
    assert (strict.status, strict.reason) == ("NO_SETUP", "BREAKOUT_BUFFER_UNCONFIRMED")


@pytest.mark.parametrize(
    "changes",
    [
        {"min_volume_ratio": -1},
        {"min_volume_ratio": "-1"},
        {"min_volume_ratio": True},
        {"min_volume_ratio": 0.5},
        {"min_volume_ratio": "NaN"},
        {"min_volume_ratio": "Infinity"},
        {"min_volume_ratio": "0.0000000000000000001"},
        {"max_extension_atr": "0"},
        {"max_atr_pct": "-0.1"},
        {"min_directional_taker_ratio": "1.01"},
        {"max_structure_age_ms": True},
        {"max_structure_age_ms": -1},
        {"max_decision_delay_ms": 86400001},
    ],
)
def test_policy_rejects_imprecise_invalid_or_unbounded_values(changes):
    with pytest.raises((ValueError, TypeError)):
        policy(**changes)


def test_no_implicit_thresholds_and_canonical_policy_values():
    with pytest.raises(TypeError):
        CandidatePolicy()
    with pytest.raises(TypeError, match="EXPLICIT_POLICY_REQUIRED"):
        build(policy={})
    settings = policy(
        min_volume_ratio="0.8000",
        min_breakout_atr="-0",
        max_structure_age_ms=0,
        max_decision_delay_ms=0,
    )
    assert settings.min_volume_ratio == "0.8" and settings.min_breakout_atr == "0"
    assert build(policy=settings).status == "CANDIDATE"


@pytest.mark.parametrize("scale", ["0.00000001", "1", "100000000"])
def test_price_scale_and_low_ambient_precision_do_not_change_structure_decision(scale):
    rows = bars(scale=scale)
    baseline = build(rows)
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.traps[Inexact] = True
        repeated = build(rows)
    assert repeated.payload() == baseline.payload()
    assert (repeated.status, repeated.side, repeated.setup) == (
        "CANDIDATE",
        "BUY",
        "BREAKOUT",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "future_minute",
        "gap",
        "unclosed_minute",
        "missing_taker",
        "missing_volume",
        "float_price",
    ],
)
def test_bad_server_facts_are_rejected_not_filled_from_tv(mutation):
    rows = bars()
    closed = rows[-1]["t"] + MINUTE
    if mutation == "future_minute":
        rows[-1]["t"] += MINUTE
    elif mutation == "gap":
        rows.pop(-2)
    elif mutation == "unclosed_minute":
        closed -= 1
    elif mutation == "missing_taker":
        rows[-1].pop("tbv")
    elif mutation == "missing_volume":
        rows[-1].pop("v")
    else:
        rows[-1]["c"] = 108.9
    with pytest.raises(ValueError):
        build(rows, closed_at_ms=closed)


@pytest.mark.parametrize("available_delta,decision_delta", [(-1, 0), (1, 0), (0, -1)])
def test_data_available_time_must_precede_decision_and_follow_close(
    available_delta, decision_delta
):
    closed = 1350 * MINUTE
    with pytest.raises(ValueError, match="NONCAUSAL_AVAILABILITY"):
        build(
            available_at_ms=closed + available_delta,
            decided_at_ms=closed + decision_delta,
        )


def test_inclusive_freshness_boundaries_and_expired_structure_are_explicit():
    closed = 1350 * MINUTE
    assert build(decided_at_ms=closed + 60000).status == "CANDIDATE"
    with pytest.raises(ValueError, match="DECISION_STALE"):
        build(decided_at_ms=closed + 60001)
    settings = policy(max_structure_age_ms=60000, max_decision_delay_ms=120000)
    assert build(policy=settings, decided_at_ms=closed + 60000).status == "CANDIDATE"
    with pytest.raises(ValueError, match="STRUCTURE_STALE"):
        build(policy=settings, decided_at_ms=closed + 60001)


def test_input_copies_and_payloads_cannot_mutate_a_frozen_decision():
    rows = bars()
    result = build(rows)
    expected = result.payload()
    rows[-1]["c"] = "999"
    result.structure["values"]["close"] = "999"
    result.payload()["policy"]["min_volume_ratio"] = "999"
    assert result.payload() == expected
    with pytest.raises(FrozenInstanceError):
        result.side = "SELL"
    with pytest.raises(FrozenInstanceError):
        result.policy.min_volume_ratio = "2"


def test_assembly_preserves_exact_plan_execution_context_and_source_snapshot_digest():
    parts = scene()
    decision, signal, _, plan, context = parts
    frozen = assemble(parts)
    assert frozen.plan.payload() == plan.payload()
    assert frozen.plan.quantity == "0.125" and frozen.plan.leverage == 3
    assert frozen.signal_digest == digest(canonical(signal.snapshot))
    assert frozen.features["execution"] == context
    assert frozen.features["candidate_builder"]["schema"] == BUILDER_VERSION
    context["policy"]["entry.reentry_enabled"] = True
    assert frozen.features["execution"]["policy"]["entry.reentry_enabled"] is False
    assert frozen.observed_at_ms == decision.observed_at_ms


@pytest.mark.parametrize(
    "which",
    [
        "environment",
        "symbol",
        "signal_observed_future",
        "signal_expired",
        "event_end",
        "decision_future",
    ],
)
def test_original_signal_scope_and_lifetime_must_bind_to_server_decision(which):
    parts = scene()
    state = parts[1]
    snapshot = state.snapshot
    if which == "environment":
        state = replace(state, environment="LIVE")
    elif which == "symbol":
        snapshot["symbol"] = "ETHUSDT"
    elif which == "signal_observed_future":
        snapshot["observed_at"] += 1
    elif which == "signal_expired":
        snapshot["expires_at_ms"] = snapshot["observed_at"] + 1
        state = replace(state, now_ms=state.now_ms + 1)
    elif which == "event_end":
        snapshot["signal"] = "EVENT_END"
    else:
        state = replace(state, now_ms=state.now_ms - 1)
    state = replace(state, snapshot_json=canonical(snapshot))
    with pytest.raises(ValueError):
        assemble(parts, signal=state)


@pytest.mark.parametrize(
    "which",
    [
        "opportunity",
        "side",
        "symbol",
        "account",
        "environment",
        "generation",
        "version",
        "instance",
        "observed",
        "decided",
        "expires",
        "config",
        "exit_handler",
    ],
)
def test_plan_must_match_structure_binding_time_and_identity_exactly(which):
    parts = scene()
    plan = parts[3]
    changes = {
        "opportunity": {"opportunity_id": "another-opportunity"},
        "side": {"side": "SELL", "stop_price": str(Decimal(plan.entry_price) + 1)},
        "symbol": {"symbol": "ETHUSDT"},
        "account": {"scope": replace(plan.scope, account_id="other")},
        "environment": {"scope": replace(plan.scope, environment="LIVE")},
        "generation": {"deployment_generation": 2},
        "version": {"code_version": "candidate-v2"},
        "instance": {"instance_id": "other-instance"},
        "observed": {"observed_at_ms": plan.observed_at_ms - 1},
        "decided": {"decided_at_ms": plan.decided_at_ms + 1},
        "expires": {"expires_at_ms": plan.expires_at_ms + 1},
        "config": {"config_digest": "a" * 64},
        "exit_handler": {"exit_handler": "another-exit"},
    }[which]
    altered = replace(plan, **changes)
    with pytest.raises(ValueError):
        assemble(parts, plan=altered, execution=safety(altered))


@pytest.mark.parametrize("which", ["absent", "changed", "schema", "mode"])
def test_binding_must_freeze_exact_policy_and_explicit_active_mode(which):
    parts = scene()
    binding = parts[2]
    config = binding.config
    if which == "absent":
        config = {}
    elif which == "changed":
        config["candidate_builder"]["policy"]["min_volume_ratio"] = "0.9"
    elif which == "schema":
        config["candidate_builder"]["schema"] = "unreviewed-v2"
    binding = replace(
        binding,
        config_json=canonical(config),
        mode="SHADOW" if which == "mode" else "ACTIVE",
    )
    plan = replace(parts[3], config_digest=binding.config_digest)
    with pytest.raises(ValueError):
        assemble(parts, binding=binding, plan=plan, execution=safety(plan))


@pytest.mark.parametrize("which", ["missing", "wrong_plan", "policy", "float"])
def test_execution_safety_context_remains_mandatory_and_plan_bound(which):
    parts = scene()
    context = deepcopy(parts[4])
    if which == "missing":
        context = None
    elif which == "wrong_plan":
        context["plan_digest"] = "a" * 64
    elif which == "policy":
        context["policy"].pop("entry.reentry_enabled")
    else:
        context["policy"]["entry.max_adverse_price_fraction"] = 0.01
    with pytest.raises(ValueError, match="EXECUTION_CONTEXT_INVALID"):
        assemble(parts, execution=context)


def test_previously_frozen_or_receipted_work_must_recover_not_rebuild():
    parts = scene()
    candidate = assemble(parts)
    for state in (
        replace(parts[1], candidate=candidate),
        replace(parts[1], receipt=EntryReceipt("IGNORED", "original terminal")),
    ):
        with pytest.raises(ValueError, match="CANDIDATE_RECOVERY_REQUIRED"):
            assemble(parts, signal=state)


def test_assembly_does_not_refresh_expired_plan_or_stale_decision():
    parts = scene()
    with pytest.raises(ValueError, match="TIME_MISMATCH"):
        assemble(parts, signal=replace(parts[1], now_ms=parts[3].expires_at_ms))
    decision = build(policy=policy(max_decision_delay_ms=10))
    parts = scene(decision)
    assert (
        assemble(parts, signal=replace(parts[1], now_ms=parts[1].now_ms + 10)).action
        == "OPEN"
    )
    with pytest.raises(ValueError, match="ASSEMBLY_STALE"):
        assemble(parts, signal=replace(parts[1], now_ms=parts[1].now_ms + 11))


def test_decision_tampering_cannot_override_derived_outcome():
    parts = scene()
    object.__setattr__(parts[0], "side", "SELL")
    with pytest.raises(ValueError, match="CANDIDATE_DECISION_MISMATCH"):
        assemble(parts)


def test_public_structure_envelope_does_not_accept_partial_or_future_metadata():
    decision = build()
    altered = decision.structure
    altered["last_closed_at_ms"] += MINUTE
    with pytest.raises(ValueError):
        StructureCandidateDecision(
            canonical(altered),
            decision.policy,
            decision.available_at_ms,
            decision.decided_at_ms,
        )
