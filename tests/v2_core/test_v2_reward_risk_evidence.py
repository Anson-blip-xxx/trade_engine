"""Unavailable move evidence must not bypass opening risk; no I/O or orders."""

import json
from copy import deepcopy

import pytest
from test_v2_directional import history, inputs, sizing

from services.v2_directional_admission import admission_decision
from services.v2_directional_replay import source_policy
from v2_core.directional import (
    evaluate_market,
    execution_market_gate,
    size_candidate,
)

UNAVAILABLE = "EXPECTED_MOVE_EVIDENCE_UNAVAILABLE"


def gate(move, *, strategy="S6", policy=None, **overrides):
    plan = evaluate_market(**inputs(strategy), policy=policy)
    sized = size_candidate(plan, **sizing(), policy=policy)
    arguments = {
        "price": "100",
        "expected_move_pct": move,
        "funding_rate": "0",
        **overrides,
    }
    return execution_market_gate(plan, sized, **arguments, policy=policy)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("zero", ["0", "0.0", "0.000000000000000000", "-0"])
@pytest.mark.parametrize("minimum_rr", ["0", "1", "2"])
def test_zero_is_not_evidence_even_with_zero_rr_threshold(strategy, zero, minimum_rr):
    assert (
        gate(zero, strategy=strategy, policy={"entry.min_reward_risk": minimum_rr})
        == UNAVAILABLE
    )


def test_none_retains_fail_closed_input_error_with_machine_reason():
    with pytest.raises(ValueError, match="^EXPECTED_MOVE_EVIDENCE_UNAVAILABLE$"):
        gate(None)


@pytest.mark.parametrize(
    "invalid",
    [
        "-1",
        "-0.000000000000000001",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "",
        True,
        0,
        0.0,
    ],
)
def test_malformed_move_is_not_coerced_to_missing_or_positive(invalid):
    with pytest.raises(ValueError):
        gate(invalid)


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize(
    "move,minimum_rr,reason",
    [
        ("0.000000000000000001", "1", "REWARD_RISK_TOO_LOW"),
        ("7.999999999999999999", "1", "REWARD_RISK_TOO_LOW"),
        ("8", "1", "MARKET_GATES_PASSED"),
        ("8.000000000000000001", "1", "MARKET_GATES_PASSED"),
        ("15.999999999999999999", "2", "REWARD_RISK_TOO_LOW"),
        ("16", "2", "MARKET_GATES_PASSED"),
        ("0.000000000000000001", "0", "MARKET_GATES_PASSED"),
    ],
)
def test_positive_exact_rr_contract_is_unchanged(strategy, move, minimum_rr, reason):
    assert (
        gate(move, strategy=strategy, policy={"entry.min_reward_risk": minimum_rr})
        == reason
    )


def test_zero_cannot_hide_invalid_other_market_facts():
    with pytest.raises(ValueError):
        gate("0", funding_rate="NaN")
    with pytest.raises(ValueError, match="stop is not on loss side"):
        gate("0", price="90")


@pytest.mark.parametrize("strategy,rate", [("S6", "0.01"), ("S8", "-0.01")])
def test_valid_target_does_not_bypass_adverse_funding(strategy, rate):
    assert gate("8", strategy=strategy, funding_rate=rate) == "ADVERSE_FUNDING"


def admission_inputs(strategy, source):
    facts = inputs(strategy)
    limits = sizing()
    for name in ("price", "atr_pct", "analysis_factor"):
        limits.pop(name)
    scope = {
        "exchange": "BINANCE",
        "account_id": "qa-risk-evidence",
        "environment": "SANDBOX",
        "product": "FUTURES",
    }
    signal = {
        "symbol": "BTCUSDT",
        "observed_at": 1,
        "expires_at_ms": 10000,
        "signal": facts["event"]["type"],
        "features": facts["event"],
    }
    context = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "assembled_at": 3,
        "valid_until_ms": 9999,
        "account_scope": scope,
        "directional": {
            "market": facts["market"],
            "price": facts["price"],
            "regime": facts["regime"],
            "short_ratio": facts["short_ratio"],
            "history": history(),
            "sizing": limits,
            "expected_move_pct": "0",
            "funding_rate": "0",
        },
    }
    config = {
        "strategy": strategy,
        "analysis_mode": "hard",
        "mode": "TESTNET_ADMISSION_ONLY",
        "account_scope": scope,
        "source": source,
        "policy": {"tv.trend_policy_enabled": True},
        "tv_start_ms": 1,
        "allowed_symbols": ["BTCUSDT"],
    }
    return signal, context, config


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_source_profile_cannot_authorize_zero_target(strategy, source):
    signal, context, config = admission_inputs(strategy, source)
    before = deepcopy((signal, context, config))
    decision = admission_decision(signal, context, config)
    assert decision.action == "IGNORED"
    assert decision.rationale == UNAVAILABLE
    assert decision.side is None and decision.quantity is None
    proof = json.loads(decision.features_json)
    assert proof["execution_market_gate"] == UNAVAILABLE
    assert proof["execution_authorized"] is False
    assert "admission_authorized" not in proof
    assert proof["source_scoring"]["profile"] == (
        "tv-trend-v1" if source == "tv_bridge" else "directional-default-v1"
    )
    assert (signal, context, config) == before


@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_positive_source_thresholds_are_not_relaxed(source):
    signal, context, config = admission_inputs("S6", source)
    policy, proof = source_policy(signal, config)
    assert policy["entry.min_reward_risk"] == "1"
    assert proof["min_score"] == (20 if source == "tv_bridge" else 30)
    context["directional"]["expected_move_pct"] = "7.999999999999999999"
    assert (
        admission_decision(signal, context, config).rationale == "REWARD_RISK_TOO_LOW"
    )
    context["directional"]["expected_move_pct"] = "8"
    decision = admission_decision(signal, context, config)
    assert decision.action == "OPEN"
    assert json.loads(decision.features_json)["execution_authorized"] is False


@pytest.mark.parametrize("missing", ["none", "field"])
def test_missing_target_is_not_synthesized_from_other_signal_facts(missing):
    signal, context, config = admission_inputs("S6", "tv_bridge")
    signal["features"].update(chg_1h="50", chg_15m="20", vol_1h="60")
    if missing == "none":
        context["directional"]["expected_move_pct"] = None
    else:
        del context["directional"]["expected_move_pct"]
    with pytest.raises(ValueError if missing == "none" else KeyError):
        admission_decision(signal, context, config)
