"""Causal entry body invalidation; no external reads, clocks or execution."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_trend_entry import rebind, scenario

from services.v2_directional_replay import (
    MARKET_ONLY_REJECTIONS,
    market_plan,
    replay_decision,
)
from services.v2_trend_entry import encoded
from v2_core.runtime_policy import resolve
from v2_core.trend_entry_quality import observe_entry_quality


def assessment(side="LONG", *, retreat="1.1", enforce=True):
    """Gap-over-level breakout: body can be erased while level still holds."""
    signal, context, config = scenario(side)
    config["policy"]["trend.breakout_body_guard_enabled"] = enforce
    values = context["directional"]["market"]["trend_structure"]["values"]
    sign = Decimal(1 if side == "LONG" else -1)
    close, atr = Decimal(values["close"]), Decimal(values["atr"])
    body = atr / 5
    values["open"] = str(close - sign * body)
    px = str(close - sign * body * Decimal(retreat))
    context["directional"]["price"] = px
    context["directional"]["market"]["15m"]["close"] = px
    # One later closed minute is now known. The completed 15m candle is fixed;
    # after rotating 1440 source minutes there are 95 complete 15m buckets.
    market = context["directional"]["market"]
    structure = market["trend_structure"]
    market["input_evidence"]["closed_at"] += 60000
    structure["closed_at_ms"] += 60000
    structure["complete_bar_count"] = 95
    structure["excluded_partial_minutes"] = 15
    context["assembled_at"] += 60000
    context["valid_until_ms"] += 60000
    signal["observed_at"] += 60000
    signal["expires_at_ms"] += 60000
    rebind(context)
    return signal, context, config


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_full_body_erased_is_terminal_local_rejection(side, source):
    signal, context, config = assessment(side)
    config["source"] = source
    frozen = deepcopy((signal, context, config))
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "TREND_BREAKOUT_BODY_INVALIDATED"
    assert plan.reason in MARKET_ONLY_REJECTIONS
    quality = proof["entry_quality"]
    assert quality["confirming_body_erased"] is True
    assert quality["mode"] == "ENFORCE_BODY_INVALIDATION"
    assert quality["execution_authorized"] is False
    assert Decimal(quality["observations"]["breakout_acceptance_atr"]) > 0
    assert Decimal(quality["observations"]["body_retrace_fraction"]) == Decimal("1.1")
    assert quality["price_observed_at_ms"] > quality["structure_closed_at_ms"]
    assert (signal, context, config) == frozen
    # The new rejection uses the existing terminal fast-consumption path. It
    # never requires account history, a model response, or another queue retry.
    context["market_only"] = True
    for key in ("history", "sizing", "funding_rate"):
        context["directional"].pop(key)
    assert replay_decision(signal, context, config).rationale == plan.reason


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("retreat", ["-0.2", "0", "0.3", "0.99"])
def test_continuation_or_partial_pullback_is_not_assumed_exhaustion(side, retreat):
    signal, context, config = assessment(side, retreat=retreat)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert proof["entry_quality"]["confirming_body_erased"] is False
    assert proof["entry_quality"]["partial_retracement_action"] == "OBSERVE_ONLY"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_exact_open_is_fully_erased_not_retained_body(side):
    signal, context, config = assessment(side, retreat="1")
    assert (
        market_plan(signal, context, config)[0].reason
        == "TREND_BREAKOUT_BODY_INVALIDATED"
    )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_default_off_preserves_old_decision_and_shadow_cannot_change_it(side):
    signal, context, config = assessment(side, enforce=False)
    baseline, original_proof = market_plan(signal, context, config)
    assert baseline.reason == "CANDIDATE"
    assert "entry_quality" not in original_proof
    config["policy"]["trend.entry_quality_shadow_enabled"] = True
    plan, proof = market_plan(signal, context, config)
    assert asdict(plan) == asdict(baseline)
    assert proof["entry_quality"]["confirming_body_erased"] is True
    assert proof["entry_quality"]["mode"] == "SHADOW"
    assert {k: v for k, v in proof.items() if k != "entry_quality"} == original_proof


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_source_price_binding_required_only_for_enforced_breakout(side):
    signal, context, config = assessment(side)
    context["directional"]["market"]["15m"].pop("close")
    rebind(context)
    assert (
        market_plan(signal, context, config)[0].reason
        == "TREND_BREAKOUT_PRICE_UNCONFIRMED"
    )
    config["policy"]["trend.breakout_body_guard_enabled"] = False
    config["policy"]["trend.entry_quality_shadow_enabled"] = True
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert proof["entry_quality"]["status"] == "UNKNOWN"


def observe_arguments():
    _, context, _ = assessment()
    market = context["directional"]["market"]
    return {
        "structure": market["trend_structure"],
        "side": "LONG",
        "price": context["directional"]["price"],
        "bound_price": market["15m"]["close"],
        "price_observed_at_ms": market["input_evidence"]["closed_at"],
        "evaluated_at_ms": context["assembled_at"],
        "market": market,
        "enforce": False,
    }


@pytest.mark.parametrize(
    "defect",
    [
        "future",
        "before_structure",
        "missing_time",
        "wrong_time_type",
        "same_clock_different_close",
        "unbound_price",
        "no_price",
        "zero_atr",
    ],
)
def test_missing_and_future_evidence_is_unknown_not_neutral(defect):
    args = observe_arguments()
    if defect == "future":
        args["price_observed_at_ms"] += 60000
    elif defect == "before_structure":
        args["price_observed_at_ms"] = args["structure"]["last_closed_at_ms"] - 60000
    elif defect == "missing_time":
        args["price_observed_at_ms"] = None
    elif defect == "wrong_time_type":
        args["price_observed_at_ms"] = str(args["price_observed_at_ms"])
    elif defect == "same_clock_different_close":
        args["price_observed_at_ms"] = args["structure"]["last_closed_at_ms"]
    elif defect == "unbound_price":
        args["bound_price"] = "1"
    elif defect == "no_price":
        args["bound_price"] = None
    else:
        args["structure"]["values"]["atr"] = "0"
    proof = observe_entry_quality(**args)
    assert proof["status"] == "UNKNOWN"
    assert "observations" not in proof
    assert "confirming_body_erased" not in proof


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_large_directional_return_is_diagnostic_not_a_gate(side):
    signal, context, config = assessment(side, retreat="0.3")
    market = context["directional"]["market"]
    market["24h"]["chg"] = "500" if side == "LONG" else "-64.44"
    rebind(context)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert (
        Decimal(proof["entry_quality"]["observations"]["directional_change_24h_pct"])
        > 60
    )
    assert proof["entry_quality"]["prior_return_action"] == "OBSERVE_ONLY"


def test_optional_shadow_field_failure_does_not_change_guard_observation():
    args = observe_arguments()
    args["market"]["24h"]["chg"] = "NaN"
    args["market"].pop("4h")
    result = observe_entry_quality(**args)
    assert result["status"] == "READY"
    assert result["observations"]["directional_change_24h_pct"] is None
    assert result["observations"]["directional_change_4h_pct"] is None


@pytest.mark.parametrize("missing", [None, [], "invalid"])
def test_invalid_optional_shadow_price_window_cannot_raise_or_block(missing):
    signal, context, config = assessment(enforce=False)
    config["policy"]["trend.entry_quality_shadow_enabled"] = True
    context["directional"]["market"]["15m"] = missing
    rebind(context)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert proof["entry_quality"]["status"] == "UNKNOWN"


def test_future_outcomes_and_signal_amplitude_do_not_enter_quality_decision():
    signal, context, config = assessment(retreat="0.3")
    before = market_plan(signal, context, config)
    signal["features"].update(chg_24h="1000", future_return="-99")
    context["future_bars"] = [{"close": "1", "time": 999999999}]
    after = market_plan(signal, context, config)
    assert before == after


def test_future_bound_market_snapshot_is_rejected_before_quality():
    signal, context, config = assessment()
    context["assembled_at"] -= 60000
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "TREND_STRUCTURE_UNCONFIRMED"
    assert "entry_quality" not in proof


def test_no_extra_quality_defaults_or_numeric_thresholds():
    policy = resolve()
    assert policy["trend.breakout_body_guard_enabled"] is False
    assert policy["trend.entry_quality_shadow_enabled"] is False
    with pytest.raises(ValueError, match="INVALID_POLICY_TYPE"):
        resolve({"trend.breakout_body_guard_enabled": "true"})


def test_enforced_quality_cannot_bypass_existing_flow_regime_or_stop_gates():
    signal, context, config = assessment("LONG", retreat="0.3")
    context["directional"]["regime"] = "risk-off"
    assert market_plan(signal, context, config)[0].reason == "regime_conflict"
    context["directional"]["regime"] = "neutral"
    config["policy"]["trend.min_stop_fraction"] = "0.001"
    config["policy"]["trend.max_stop_fraction"] = "0.001"
    assert market_plan(signal, context, config)[0].reason == "TREND_STOP_TOO_WIDE"


def test_partial_body_pullback_case_remains_observation_not_calibrated_rule():
    args = observe_arguments()
    args["structure"]["values"].update(
        open="0.333",
        close="0.365",
        high="0.369",
        low="0.331",
        atr="0.013122",
        ema20="0.332",
        ema60="0.321",
        prior20_high="0.342",
    )
    args["price"] = args["bound_price"] = "0.354"
    args["enforce"] = True
    proof = observe_entry_quality(**args)
    assert proof["status"] == "READY"
    assert proof["confirming_body_erased"] is False
    assert proof["observations"]["body_retrace_fraction"] == "0.34375"
    assert proof["partial_retracement_action"] == "OBSERVE_ONLY"


def test_pullback_resume_is_not_silently_subject_to_breakout_body_rule():
    signal, context, config = scenario("LONG")
    config["policy"]["trend.breakout_body_guard_enabled"] = True
    values = context["directional"]["market"]["trend_structure"]["values"]
    values.update(
        open="139.9",
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
    context["directional"]["price"] = "139.7"
    context["directional"]["market"]["15m"]["close"] = "139.7"
    market = context["directional"]["market"]
    market["input_evidence"]["closed_at"] += 60000
    market["trend_structure"]["closed_at_ms"] += 60000
    market["trend_structure"]["complete_bar_count"] = 95
    market["trend_structure"]["excluded_partial_minutes"] = 15
    context["assembled_at"] += 60000
    rebind(context)
    plan, proof = market_plan(signal, context, config)
    assert plan.reason == "CANDIDATE"
    assert plan.entry_mode == "TREND_PULLBACK_RESUME"
    assert proof["entry_quality"]["confirming_body_erased"] is False
