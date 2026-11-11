"""Same-input structure admission; no venue calls or implicit execution."""

import json
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_directional_replay import SCOPE

from services.v2_directional_admission import signal_only_rejection
from services.v2_directional_replay import market_plan, replay_decision, source_policy
from services.v2_s3_candles import STRUCTURE_VERSION, build_frame
from services.v2_trend_entry import evaluate_trend_market, trend_reward_gate
from v2_core.directional import evaluate_market, size_candidate
from v2_core.producer import market_envelope
from v2_core.runtime_policy import resolve


def scenario(side="SHORT", source="s3"):
    rows = []
    sign = 1 if side == "LONG" else -1
    for minute in range(1440):
        opened = Decimal(100) + sign * Decimal(".03") * minute
        closed = opened + sign * Decimal(".03")
        rows.append(
            {
                "t": minute * 60000,
                "o": str(opened),
                "c": str(closed),
                "h": str(max(opened, closed) + Decimal(".01")),
                "l": str(min(opened, closed) - Decimal(".01")),
                "v": "10",
                "tbv": "6" if side == "LONG" else "4",
            }
        )
    now = 86400000
    frame = build_frame(
        {
            "source": "BINANCE_FUTURES",
            "environment": "SANDBOX",
            "interval": "1m",
            "closed_at": now,
            "candles": {"BTCUSDT": list(reversed(rows))},
        },
        environment="SANDBOX",
        feature_version=STRUCTURE_VERSION,
    )
    market = frame["windows"]["BTCUSDT"]
    signal = {
        "symbol": "BTCUSDT",
        "signal": "TREND_UP" if side == "LONG" else "TREND_DOWN",
        "observed_at": now,
        "expires_at_ms": now + 90000,
        "features": {"strength": 20, "chg_1h": "-1", "chg_4h": "-2"},
    }
    limits = sizing()
    for key in ("price", "atr_pct", "analysis_factor"):
        limits.pop(key)
    context = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": now + 1,
        "valid_until_ms": now + 60000,
        "account_scope": asdict(SCOPE),
        "sources": {"s3": market_envelope("s3", "SANDBOX", "BTCUSDT", now, market)},
        "directional": {
            "market": market,
            "price": market["15m"]["close"],
            "regime": "neutral",
            "short_ratio": "0.5",
            "history": history(),
            "sizing": limits,
            "expected_move_pct": "0",
            "funding_rate": "0",
        },
    }
    config = {
        "strategy": "S6" if side == "LONG" else "S8",
        "source": source,
        "policy": {
            "entry.trend_structure_enabled": True,
            "entry.force_isolated": True,
            "sizing.pool_fraction": "0.70",
            "sizing.max_margin_fraction": "0.10",
        },
        "mode": "REPLAY_ONLY",
        "account_scope": asdict(SCOPE),
        "analysis_mode": "hard",
    }
    return signal, context, config


def rebind(context):
    market = context["directional"]["market"]
    context["sources"]["s3"] = market_envelope(
        "s3", "SANDBOX", "BTCUSDT", market["input_evidence"]["closed_at"], market
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_early_trend_gets_structural_evaluation_without_raw_amplitude_barrier(
    side, source
):
    signal, context, config = scenario(side, source)
    original = deepcopy((signal, context, config))
    assert signal_only_rejection(signal, config) is None
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert plan.entry_mode == "TREND_BREAKOUT"
    assert plan.margin_mode == "ISOLATED" and plan.leverage == 2
    assert Decimal(plan.stop_fraction) < Decimal(".08")
    assert proof["reward_estimate_kind"] == "ATR_TARGET_NOT_FORECAST"
    result = replay_decision(signal, context, config)
    assert result.rationale == "DIRECTIONAL_REPLAY_ONLY"
    evaluation = json.loads(result.features_json)
    assert evaluation["execution_market_gate"] == "MARKET_GATES_PASSED"
    assert evaluation["execution_authorized"] is False
    assert Decimal(evaluation["sizing"]["notional"]) / plan.leverage <= Decimal(100)
    assert (signal, context, config) == original


def test_profile_off_keeps_raw_strength_gate_and_old_evaluator_cannot_bypass_profile():
    signal, _, config = scenario()
    config["policy"]["entry.trend_structure_enabled"] = False
    assert signal_only_rejection(signal, config).rationale == "strength_below_minimum"
    assert resolve()["entry.trend_structure_enabled"] is False
    with pytest.raises(ValueError, match="VERIFIED_TREND_STRUCTURE_PROFILE_REQUIRED"):
        evaluate_market(**inputs(), policy={"entry.trend_structure_enabled": True})


@pytest.mark.parametrize(
    "defect", ["missing", "legacy", "scope", "time", "warmup", "unbound"]
)
def test_bad_structure_is_terminal_local_rejection(defect):
    signal, context, config = scenario()
    market = context["directional"]["market"]
    if defect == "missing":
        market.pop("trend_structure")
    elif defect == "legacy":
        market["input_evidence"]["window_version"] = "s3-closed-1m-v5"
    elif defect == "scope":
        market["trend_structure"]["environment"] = "LIVE"
    elif defect == "time":
        market["trend_structure"]["closed_at_ms"] += 60000
    elif defect == "warmup":
        market["trend_structure"]["indicator_bar_count"] = 12
    else:
        context["sources"]["s3"]["symbol"] = "ETHUSDT"
    plan, _ = market_plan(signal, context, config)
    assert plan.reason == "TREND_STRUCTURE_UNCONFIRMED"
    context["market_only"] = True
    assert replay_decision(signal, context, config).rationale == plan.reason


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("taker_buy_ratio", "0.8", "TREND_FLOW_UNCONFIRMED"),
        ("volume_ratio", "0.1", "TREND_VOLUME_UNCONFIRMED"),
        ("ema20_slope", "0.01", "TREND_NOT_ALIGNED"),
    ],
)
def test_high_strength_does_not_override_conflicting_evidence(field, value, reason):
    signal, context, config = scenario()
    signal["features"]["strength"] = 99
    context["directional"]["market"]["trend_structure"]["values"][field] = value
    if field == "volume_ratio":
        context["directional"]["market"]["trend_structure"]["values"]["volume"] = "15"
    rebind(context)
    assert market_plan(signal, context, config)[0].reason == reason


def test_observed_zero_taker_ratio_is_valid_not_missing():
    signal, context, config = scenario()
    context["directional"]["market"]["trend_structure"]["values"]["taker_buy_ratio"] = (
        "0"
    )
    rebind(context)
    assert market_plan(signal, context, config)[0].reason == "CANDIDATE"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_drift_past_breakout_and_late_chase_are_rejected(side):
    signal, context, config = scenario(side)
    v = context["directional"]["market"]["trend_structure"]["values"]
    sign = 1 if side == "LONG" else -1
    context["directional"]["price"] = str(
        Decimal(v["close"]) + sign * Decimal(v["atr"]) * 3
    )
    assert market_plan(signal, context, config)[0].reason == "TREND_ENTRY_MOVED"
    context["directional"]["price"] = v[
        "prior20_high" if side == "LONG" else "prior20_low"
    ]
    assert market_plan(signal, context, config)[0].reason == "TREND_ENTRY_MOVED"


def test_stop_is_not_shortened_to_fit_cap_and_costs_reduce_planned_reward():
    signal, context, config = scenario()
    config["policy"]["trend.max_stop_fraction"] = "0.005"
    assert market_plan(signal, context, config)[0].reason == "TREND_STOP_TOO_WIDE"
    config["policy"].pop("trend.max_stop_fraction")
    config["policy"]["trend.roundtrip_cost_fraction"] = "0.05"
    assert (
        market_plan(signal, context, config)[0].reason
        == "TREND_PLANNED_REWARD_RISK_TOO_LOW"
    )


def test_funding_and_account_budget_still_gate_full_replay():
    signal, context, config = scenario()
    context["directional"]["funding_rate"] = "-0.01"
    assert replay_decision(signal, context, config).rationale == "ADVERSE_FUNDING"
    context["directional"]["funding_rate"] = "0"
    context["directional"]["sizing"]["available_margin"] = "0"
    assert replay_decision(signal, context, config).rationale == "INSUFFICIENT_BUDGET"


def test_structure_age_and_existing_regime_veto():
    signal, context, config = scenario("LONG")
    context["directional"]["regime"] = "risk-off"
    assert market_plan(signal, context, config)[0].reason == "regime_conflict"
    context["directional"]["regime"] = "neutral"
    context["assembled_at"] += 960000
    assert market_plan(signal, context, config)[0].reason == "TREND_STRUCTURE_STALE"


def test_net_reward_gate_rechecks_actual_tick_stop_and_never_uses_realized_signal_move():
    signal, context, config = scenario()
    plan, proof = evaluate_trend_market(signal, context, config)
    settings, _ = source_policy(signal, config)
    sized = size_candidate(
        plan, **sizing(price=context["directional"]["price"]), policy=settings
    )
    assert (
        trend_reward_gate(
            plan,
            sized,
            price=context["directional"]["price"],
            proof=proof,
            policy=settings,
        )
        == "MARKET_GATES_PASSED"
    )
    proof["target_price"] = context["directional"]["price"]
    assert (
        trend_reward_gate(
            plan,
            sized,
            price=context["directional"]["price"],
            proof=proof,
            policy=settings,
        )
        == "REWARD_RISK_TOO_LOW"
    )


def test_cost_floor_applies_to_sizing_but_does_not_mutate_policy_or_quant():
    signal, _, config = scenario()
    config["policy"]["sizing.cost_buffer_fraction"] = "0"
    settings, _ = source_policy(signal, config)
    assert settings["sizing.cost_buffer_fraction"] == "0.002"
    assert config["policy"]["sizing.cost_buffer_fraction"] == "0"
    config["source"] = "quant"
    assert source_policy(signal, config)[0]["sizing.cost_buffer_fraction"] == "0"


def test_inverted_stop_range_is_rejected():
    with pytest.raises(ValueError, match="INVERTED_TREND_STOP_RANGE"):
        resolve({"trend.min_stop_fraction": "0.02", "trend.max_stop_fraction": "0.01"})


def test_pullback_resume_is_distinct_from_breakout_and_no_setup_is_not_a_retry():
    from services.v2_trend_entry import encoded

    signal, context, config = scenario("LONG")
    values = context["directional"]["market"]["trend_structure"]["values"]
    values.update(
        open="139",
        high="140.1",
        low="138.9",
        close="140",
        previous_open="139.8",
        previous_close="139",
        previous_high="139.9",
        previous_low="138.8",
        ema20="139.5",
        ema60="135",
        ema20_slope="0.1",
        ema60_slope="0.1",
        atr="1",
        atr_pct=encoded(Decimal(100) / Decimal(140)),
        prior3_low="138.8",
        prior_swing_low="138.8",
        breakout_long=False,
        prior3_touched_ema20_long=True,
    )
    context["directional"]["price"] = "140"
    rebind(context)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert plan.entry_mode == "TREND_PULLBACK_RESUME"
    assert Decimal(proof["target_price"]) <= Decimal(values["prior20_high"])
    values["prior3_touched_ema20_long"] = False
    rebind(context)
    context["market_only"] = True
    assert replay_decision(signal, context, config).rationale == "TREND_NO_FRESH_SETUP"


def test_same_hour_fall_does_not_confirm_an_upward_rebound_short():
    signal, context, config = scenario()
    signal["features"].update(strength=99, chg_1h="-15")
    values = context["directional"]["market"]["trend_structure"]["values"]
    values["open"] = str(Decimal(values["close"]) - Decimal("0.005"))
    rebind(context)
    assert market_plan(signal, context, config)[0].reason == "TREND_COUNTERMOVE"


def test_tv_metadata_cannot_substitute_for_server_structure():
    signal, context, config = scenario(source="tv_bridge")
    signal["features"]["tv_producer"] = {
        "setup": "TREND_BREAKOUT",
        "version": "tv-causal-15m-v2",
    }
    context["directional"]["market"].pop("trend_structure")
    rebind(context)
    assert (
        market_plan(signal, context, config)[0].reason == "TREND_STRUCTURE_UNCONFIRMED"
    )


def test_fusion_confirmation_does_not_expand_base_allocation_and_conflict_vetoes():
    from v2_core.signal_fusion import FusionPolicy, build_fusion

    signal, context, config = scenario()
    peer = {
        "signal_id": "qa-tv-trend",
        "source": "tv_bridge",
        "environment": "SANDBOX",
        "symbol": signal["symbol"],
        "signal": signal["signal"],
        "observed_at": signal["observed_at"],
        "expires_at_ms": signal["expires_at_ms"],
    }
    context["signal_fusion"] = build_fusion(
        signal,
        source="s3",
        peers=[peer],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=context["assembled_at"],
    )
    result = json.loads(replay_decision(signal, context, config).features_json)
    assert result["market_plan"]["score"] == 65
    assert result["signal_fusion"]["allocation_score"] == 60
    peer["signal"] = "TREND_UP"
    context["signal_fusion"] = build_fusion(
        signal,
        source="s3",
        peers=[peer],
        policy=FusionPolicy(enabled=True),
        policy_version=1,
        now_ms=context["assembled_at"],
    )
    assert replay_decision(signal, context, config).rationale == "TV_DIRECTION_CONFLICT"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("ceiling,expected", [(4, 3), (2, 2), (1, None)])
def test_adaptive_profile_ceiling_only_selects_configured_safe_tiers(
    side, ceiling, expected
):
    from test_v2_adaptive_leverage import facts

    signal, context, config = scenario(side)
    config["policy"].update(
        {
            "leverage.adaptive_enabled": True,
            "capital.enabled": True,
            "portfolio_risk.enabled": True,
            "trend.max_leverage": ceiling,
            "trend.base_score": 90,
        }
    )
    context["directional"]["leverage_venue"] = facts()
    result = replay_decision(signal, context, config)
    proof = json.loads(result.features_json)
    assert proof["leverage_decision"]["desired"] == 5
    assert proof["leverage_decision"]["selected"] == expected
    assert proof["leverage_decision"]["profile_leverage_cap"] == ceiling
    if expected is None:
        assert result.rationale == "NO_VERIFIED_SAFE_LEVERAGE"
        assert "sizing" not in proof
    else:
        assert result.rationale == "DIRECTIONAL_REPLAY_ONLY"
        assert proof["market_plan"]["leverage"] == expected
        assert proof["execution_authorized"] is False
