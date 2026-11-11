"""Frozen setup research uses real pure evaluations; no orders or database."""

import json
from copy import deepcopy
from decimal import Decimal, localcontext

import pytest
from test_v2_mover_gate_shadow import frozen_sample

import services.v2_mover_gate_shadow as mover
from services.v2_setup_shadow import evaluate_setup_shadow
from services.v2_trend_entry import encoded, evaluate_trend_market
from v2_core.evidence import canonical, digest
from v2_core.producer import market_envelope


def rebind(sample):
    context = sample["context"]
    market = context["directional"]["market"]
    context["sources"]["s3"] = market_envelope(
        "s3", "SANDBOX", "BTCUSDT", market["input_evidence"]["closed_at"], market
    )


def setup_sample(*, side="LONG", setup="BREAKOUT", volume="8", regime="neutral"):
    sample = frozen_sample(
        side=side,
        volume=volume,
        buy_fraction="0.6" if side == "LONG" else "0.4",
        regime=regime,
    )
    values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
    if setup in {"PULLBACK_RESUME", "NO_FRESH_SETUP"}:
        if side == "LONG":
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
                prior3_touched_ema20_long=setup == "PULLBACK_RESUME",
            )
        else:
            values.update(
                open="61",
                high="61.1",
                low="59.9",
                close="60",
                previous_open="60.2",
                previous_close="61",
                previous_high="61.2",
                previous_low="60.1",
                ema20="60.5",
                ema60="65",
                ema20_slope="-0.1",
                ema60_slope="-0.1",
                atr="1",
                atr_pct=encoded(Decimal(100) / Decimal(60)),
                prior3_high="61.2",
                prior_swing_high="61.2",
                breakout_short=False,
                prior3_touched_ema20_short=setup == "PULLBACK_RESUME",
            )
        sample["context"]["directional"]["price"] = values["close"]
        rebind(sample)
    return sample


def evaluate(sample, **overrides):
    arguments = {
        "environment": "SANDBOX",
        "evidence_ref": sample["evidence_ref"],
        "config_digest": digest(canonical(sample["config"])),
        **overrides,
    }
    return evaluate_setup_shadow(
        sample["signal"], sample["context"], sample["config"], **arguments
    )


@pytest.mark.parametrize("side,output_side", [("LONG", "BUY"), ("SHORT", "SELL")])
@pytest.mark.parametrize("setup", ["BREAKOUT", "PULLBACK_RESUME"])
def test_each_setup_relaxes_only_its_registered_alternative(side, output_side, setup):
    sample = setup_sample(side=side, setup=setup)
    original = deepcopy(sample)
    result = evaluate(sample)
    assert result["status"] == "READY"
    assert result["setup"] == setup
    assert result["side"] == output_side
    assert result["regime"] == "neutral"
    assert result["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert result["global_volume075_market_reason"] == "CANDIDATE"
    target = "BREAKOUT_VOLUME075" if setup == "BREAKOUT" else "PULLBACK_VOLUME075"
    for name, alternative in result["alternatives"].items():
        assert alternative["status"] == "READY"
        assert alternative["min_volume_ratio"] == ("0.75" if name == target else "1")
        assert alternative["market_reason"] == (
            "CANDIDATE" if name == target else "TREND_VOLUME_UNCONFIRMED"
        )
        assert alternative["market_reason_changed"] is (name == target)
    assert result["bar_close_ms"] == result["observed_ms"] == 86400000
    assert result["decided_ms"] is None
    assert result["volume_ratio"] == "0.8"
    assert result["execution_authorized"] is False
    assert result["automatic_parameter_updates"] is False
    assert result["promotion_authorized"] is False
    assert result["not_trade_prediction"] is True
    assert sample == original


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_breakout_has_precedence_when_both_structural_predicates_are_true(side):
    sample = setup_sample(side=side)
    values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
    suffix = "long" if side == "LONG" else "short"
    values["prior3_touched_ema20_" + suffix] = True
    values["previous_open"] = values[
        "previous_high" if side == "LONG" else "previous_low"
    ]
    rebind(sample)
    result = evaluate(sample)
    assert result["status"] == "READY"
    assert result["setup"] == "BREAKOUT"
    assert result["alternatives"]["BREAKOUT_VOLUME075"]["market_reason"] == "CANDIDATE"
    assert result["alternatives"]["PULLBACK_VOLUME075"]["min_volume_ratio"] == "1"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("boundary", ["doji", "previous_same_direction", "equal_cross"])
def test_pullback_resumed_boundaries_match_the_actual_evaluator(side, boundary):
    sample = setup_sample(side=side, setup="PULLBACK_RESUME")
    values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
    if boundary == "doji":
        values["previous_open"] = values["previous_close"]
    elif boundary == "previous_same_direction":
        values["previous_open"] = values[
            "previous_low" if side == "LONG" else "previous_high"
        ]
    else:
        values["previous_high" if side == "LONG" else "previous_low"] = values["close"]
    rebind(sample)
    result = evaluate(sample)
    candidate_config = deepcopy(sample["config"])
    candidate_config["policy"]["trend.min_volume_ratio"] = "0.75"
    plan, proof = evaluate_trend_market(
        sample["signal"], sample["context"], candidate_config
    )
    assert result["status"] == "READY"
    assert result["global_volume075_market_reason"] == plan.reason
    assert result["setup"] == (
        proof["setup"] if boundary == "doji" else "NO_FRESH_SETUP"
    )
    if boundary != "doji":
        assert plan.reason == "TREND_NO_FRESH_SETUP"
        assert all(
            row["min_volume_ratio"] == "1" for row in result["alternatives"].values()
        )


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_no_fresh_setup_retains_baseline_for_both_alternatives(side):
    result = evaluate(setup_sample(side=side, setup="NO_FRESH_SETUP"))
    assert result["setup"] == "NO_FRESH_SETUP"
    assert result["global_volume075_market_reason"] == "TREND_NO_FRESH_SETUP"
    assert all(
        row["market_reason"] == "TREND_VOLUME_UNCONFIRMED"
        and row["min_volume_ratio"] == "1"
        and row["market_reason_changed"] is False
        for row in result["alternatives"].values()
    )


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("regime", "regime_conflict"),
        ("flow", "TREND_FLOW_UNCONFIRMED"),
        ("alignment", "TREND_NOT_ALIGNED"),
        ("late_price", "TREND_ENTRY_MOVED"),
    ],
)
def test_setup_description_cannot_override_other_market_vetoes(defect, reason):
    sample = setup_sample(regime="risk-off" if defect == "regime" else "neutral")
    values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
    if defect == "flow":
        values["taker_buy_ratio"] = "0.1"
    elif defect == "alignment":
        values["ema20_slope"] = "-1"
    elif defect == "late_price":
        sample["context"]["directional"]["price"] = str(Decimal(values["close"]) + 3)
    rebind(sample)
    result = evaluate(sample)
    assert result["status"] == "READY"
    assert result["setup"] == "BREAKOUT"
    assert result["global_volume075_market_reason"] == reason
    assert result["alternatives"]["BREAKOUT_VOLUME075"]["market_reason"] == reason
    assert all(
        row["market_reason"] != "CANDIDATE" for row in result["alternatives"].values()
    )


def test_two_actual_evaluations_only_and_no_input_mutation(monkeypatch):
    sample = setup_sample()
    original = deepcopy(sample)
    calls = []

    def counted(signal, context, config):
        calls.append(deepcopy((signal, context, config)))
        return evaluate_trend_market(signal, context, config)

    monkeypatch.setattr(mover, "evaluate_trend_market", counted)
    result = evaluate(sample)
    assert result["status"] == "READY"
    assert len(calls) == 2
    assert calls[0][:2] == calls[1][:2] == (sample["signal"], sample["context"])
    expected = deepcopy(calls[0][2])
    expected["policy"]["trend.min_volume_ratio"] = "0.75"
    assert calls[1][2] == expected
    assert sample == original


def test_setup_research_exposes_legacy_adapter_without_mutating_history():
    sample = setup_sample()
    for key in (
        "trend.breakout_body_guard_enabled",
        "trend.entry_quality_shadow_enabled",
    ):
        sample["config"]["policy"].pop(key)
    original = deepcopy(sample)
    result = evaluate(sample)
    assert result["status"] == "READY"
    assert result["policy_compatibility"]["mode"] == "EXPLICIT_LEGACY_SCHEMA_ADAPTER"
    assert result["policy_compatibility"]["source_config_digest"] == digest(
        canonical(sample["config"])
    )
    assert result["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert sample == original


@pytest.mark.parametrize("defect", ["partial_policy", "threshold", "missing_threshold"])
def test_incomplete_or_out_of_plan_frozen_policy_never_uses_defaults(
    defect, monkeypatch
):
    sample = setup_sample()
    if defect == "partial_policy":
        sample["config"]["policy"].pop("trend.max_atr_pct")
    elif defect == "threshold":
        sample["config"]["policy"]["trend.min_volume_ratio"] = "0.8"
    else:
        sample["config"]["policy"].pop("trend.min_volume_ratio")
    monkeypatch.setattr(
        mover, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    result = evaluate(sample)
    assert result["status"] == "UNKNOWN"
    assert result["setup"] is None
    assert all(row["market_reason"] is None for row in result["alternatives"].values())
    if defect == "threshold":
        assert result["reason"] == "BASELINE_OUTSIDE_FROZEN_STUDY_PLAN"


@pytest.mark.parametrize(
    "defect",
    [
        "missing_structure",
        "missing_value",
        "nan",
        "infinity",
        "float",
        "false_string",
        "integer_bool",
        "unbound",
        "source_tamper",
        "stale",
        "symbol",
        "scope",
        "market_scope",
        "market_host",
        "account_scope",
        "timestamp_bool",
    ],
)
def test_missing_invalid_or_tampered_frozen_inputs_are_unknown(defect):
    sample = setup_sample()
    context = sample["context"]
    market = context["directional"]["market"]
    values = market["trend_structure"]["values"]
    if defect == "missing_structure":
        market.pop("trend_structure")
    elif defect == "missing_value":
        values.pop("previous_close")
    elif defect in {"nan", "infinity", "float"}:
        values["atr_pct"] = {"nan": "NaN", "infinity": "Infinity", "float": 0.1}[defect]
    elif defect in {"false_string", "integer_bool"}:
        values["breakout_long"] = "false" if defect == "false_string" else 1
    elif defect == "unbound":
        context["sources"]["s3"]["symbol"] = "ETHUSDT"
    elif defect == "source_tamper":
        context["sources"]["s3"]["features"] = deepcopy(market)
        context["sources"]["s3"]["features"]["trend_structure"]["values"]["ema20"] = "1"
    elif defect == "stale":
        context["assembled_at"] += 960000
        sample["signal"]["expires_at_ms"] += 960000
    elif defect == "symbol":
        sample["signal"]["symbol"] = "ETHUSDT"
    elif defect == "scope":
        context["environment"] = "LIVE"
    elif defect == "market_scope":
        market["input_evidence"]["environment"] = "LIVE"
    elif defect == "market_host":
        market["input_evidence"]["market_host"] = "fapi.binance.com"
    elif defect == "account_scope":
        sample["config"]["account_scope"] = {
            "environment": "LIVE",
            "account_id": "PRIVATE",
        }
    else:
        sample["signal"]["observed_at"] = True
    original = deepcopy(sample)
    result = evaluate(sample)
    assert result["status"] == "UNKNOWN"
    assert all(row["market_reason"] is None for row in result["alternatives"].values())
    assert "PRIVATE" not in json.dumps(result)
    assert sample == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "LIVE"},
        {"environment": "TEST"},
        {"environment": False},
        {"environment": {}},
        {"config_digest": "b" * 64},
        {"config_digest": None},
        {"config_digest": "SECRET"},
        {"evidence_ref": None},
        {"evidence_ref": "SECRET"},
    ],
)
def test_explicit_environment_and_frozen_hashes_are_required(overrides):
    result = evaluate(setup_sample(), **overrides)
    assert result["status"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(result)
    assert all(row["market_reason"] is None for row in result["alternatives"].values())


def test_missing_flow_is_unknown_but_real_zero_flow_is_preserved():
    missing = evaluate(setup_sample(volume="0"))
    assert missing["status"] == "UNKNOWN"
    assert missing["reason"] == "MISSING_FROZEN_FLOW"
    assert missing["taker_buy_ratio"] is None
    assert missing["global_volume075_market_reason"] == "TREND_FLOW_UNCONFIRMED"
    assert all(row["market_reason"] is None for row in missing["alternatives"].values())
    sample = setup_sample(side="SHORT")
    sample["context"]["directional"]["market"]["trend_structure"]["values"][
        "taker_buy_ratio"
    ] = "0"
    rebind(sample)
    zero = evaluate(sample)
    assert zero["status"] == "READY"
    assert zero["taker_buy_ratio"] == "0"
    assert zero["global_volume075_market_reason"] == "CANDIDATE"


def test_missing_volume_reference_cannot_generate_alternative_candidates():
    sample = setup_sample()
    values = sample["context"]["directional"]["market"]["trend_structure"]["values"]
    values.update(prior20_mean_volume="0", volume_ratio=None)
    rebind(sample)
    result = evaluate(sample)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == "MISSING_VOLUME_RATIO"
    assert all(row["market_reason"] is None for row in result["alternatives"].values())


def test_output_is_bounded_whitelisted_and_independent_of_decimal_context():
    sample = setup_sample(setup="PULLBACK_RESUME")
    sample["config"]["account_scope"] = {
        "environment": "SANDBOX",
        "account_id": "PRIVATE_ACCOUNT",
    }
    sample["context"]["memo"] = "PRIVATE_CONTEXT"
    original = deepcopy(sample)
    normal = evaluate(sample)
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        low_precision = evaluate(sample)
    assert normal == low_precision
    assert set(normal) == {
        "status",
        "reason",
        "regime",
        "setup",
        "side",
        "bar_close_ms",
        "observed_ms",
        "decided_ms",
        "volume_ratio",
        "taker_buy_ratio",
        "atr_pct",
        "baseline_market_reason",
        "global_volume075_market_reason",
        "alternatives",
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
        "not_trade_prediction",
        "policy_compatibility",
    }
    encoded_result = json.dumps(normal, allow_nan=False)
    assert len(encoded_result) < 100000
    assert (
        "PRIVATE_ACCOUNT" not in encoded_result
        and "PRIVATE_CONTEXT" not in encoded_result
    )
    assert sample == original
