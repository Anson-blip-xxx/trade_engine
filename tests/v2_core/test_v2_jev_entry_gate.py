"""Pure experimental gate QA; no DB, API, queue or production wiring."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from v2_core.jev_advisory import _result, parse_response, prepare_request
from v2_core.jev_entry_gate import JevEntryGatePolicy, evaluate_entry_gate
from v2_core.strategy import StrategyDecision


@pytest.fixture
def case():
    features = {
        "symbol": "BTCUSDT",
        "observed_at_ms": 100000,
        "timeframe": "15m",
        "direction": "LONG",
        "price": "100",
        "atr_pct": "1.2",
        "rsi_15m": "62",
    }
    prepared = prepare_request(features, now_ms=100100, max_age_ms=60000)
    response = parse_response(
        {
            "model": "jev-1.13.0",
            "answers": {
                "trend_supported": {"type": "noul", "noul": "0.8"},
                "chase_risk": {"type": "noul", "noul": "0.2"},
                "liquidity_fragile": {"type": "noul", "noul": "1"},
            },
            "usage": {"input_tokens": 900, "output_tokens": 60},
        }
    )
    return {
        "decision": StrategyDecision(
            "OPEN",
            "DIRECTIONAL_MARKET_GATES_PASSED",
            side="BUY",
            quantity="1.25",
            features_json=json.dumps(
                {
                    "admission_authorized": True,
                    "execution_authorized": False,
                    "sizing": {"quantity": "1.25", "leverage": 3},
                }
            ),
        ),
        "symbol": "BTCUSDT",
        "public_features": features,
        "advisory": _result(
            "ADVISORY_READY",
            "MODEL_OBSERVED",
            prepared,
            now_ms=100200,
            expires_at_ms=130200,
            response=response,
        ),
        "now_ms": 100300,
        "started_at_ms": 100100,
        "deadline_ms": 105100,
        "signal_expires_at_ms": 120000,
        "policy": JevEntryGatePolicy(enabled=True),
    }


def rejected(result, *, status="SKIP"):
    assert result.status == status
    assert result.decision.action == "IGNORED"
    assert result.decision.side is None and result.decision.quantity is None
    evidence = json.loads(result.evidence_json)
    assert evidence["execution_authorized"] is False
    assert evidence["risk_override"] is False
    features = json.loads(result.decision.features_json)
    assert features["admission_authorized"] is False
    assert features["execution_authorized"] is False


def test_pass_preserves_original_order_terms_and_inputs(case):
    before = deepcopy(case)
    result = evaluate_entry_gate(**case)
    assert result.status == "PASS"
    assert result.decision is case["decision"]
    assert result.decision.quantity == "1.25"
    assert case == before
    evidence = json.loads(result.evidence_json)
    assert evidence["execution_authorized"] is False
    assert evidence["liquidity_use"] == "NOT_USED"
    assert evidence["threshold_semantics"] == "EXPERIMENTAL_UNCALIBRATED_FILTER"
    assert evidence["probability_semantics"] == "NOT_TRADE_WIN_RATE"
    assert evidence["valid_until_ms"] == case["deadline_ms"]
    assert evidence["model_expires_at_ms"] == case["advisory"]["expires_at_ms"]


def test_cached_pass_cannot_outlive_an_earlier_model_expiry(case):
    case["advisory"]["expires_at_ms"] = case["now_ms"] + 1
    result = evaluate_entry_gate(**case)
    assert result.status == "PASS"
    assert json.loads(result.evidence_json)["valid_until_ms"] == case["now_ms"] + 1


def test_default_off_does_not_inspect_model_features_or_clock(case):
    original = case["decision"]
    for key in case:
        if key not in {"decision", "policy"}:
            case[key] = object()
    case.pop("policy")
    result = evaluate_entry_gate(**case)
    assert result.status == "PASS" and result.reason == "JEV_GATE_DISABLED"
    assert result.decision is original
    assert json.loads(result.evidence_json)["valid_until_ms"] is None


@pytest.mark.parametrize(
    "reason", ["INSUFFICIENT_BUDGET", "ADVERSE_FUNDING", "LOW_QUALITY"]
)
def test_preexisting_risk_reject_is_preserved_even_with_bad_model_and_policy(
    case, reason
):
    original = StrategyDecision("IGNORED", reason)
    case.update(decision=original, policy=object(), advisory=object(), now_ms=object())
    result = evaluate_entry_gate(**case)
    assert result.status == "VETO" and result.reason == "PREEXISTING_REJECTION"
    assert result.decision is original


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("trend_supported", "0.599", "JEV_TREND_UNSUPPORTED"),
        ("chase_risk", "0.701", "JEV_CHASE_RISK_HIGH"),
    ],
)
def test_unfavourable_model_vetoes_only_original_open(case, field, value, reason):
    case["advisory"]["hypotheses"][field] = value
    result = evaluate_entry_gate(**case)
    rejected(result, status="VETO")
    assert result.reason == reason
    assert case["decision"].action == "OPEN"


def test_thresholds_are_configurable_and_boundary_is_explicit(case):
    case["advisory"]["hypotheses"].update(trend_supported="0.6", chase_risk="0.7")
    assert evaluate_entry_gate(**case).status == "PASS"
    case["policy"] = replace(case["policy"], min_trend_support="0.61")
    assert evaluate_entry_gate(**case).reason == "JEV_TREND_UNSUPPORTED"
    case["policy"] = replace(
        case["policy"], min_trend_support="0.6", max_chase_risk="0.69"
    )
    assert evaluate_entry_gate(**case).reason == "JEV_CHASE_RISK_HIGH"


@pytest.mark.parametrize("score", ["0", "0.5", "1"])
def test_liquidity_hypothesis_does_not_claim_missing_depth_authority(case, score):
    case["advisory"]["hypotheses"]["liquidity_fragile"] = score
    assert "spread_bps" not in case["public_features"]
    assert evaluate_entry_gate(**case).status == "PASS"


@pytest.mark.parametrize(
    "advisory",
    [
        None,
        {"status": "FALLBACK", "reason": "CACHE_MISS"},
        {"status": "FALLBACK", "reason": "BUSY"},
    ],
)
def test_missing_result_is_bounded_pending_without_open_decision(case, advisory):
    case["advisory"] = advisory
    result = evaluate_entry_gate(**case)
    assert result.status == "PENDING" and result.decision is None
    case["now_ms"] = case["deadline_ms"]
    expired = evaluate_entry_gate(**case)
    rejected(expired)
    assert expired.reason == "JEV_REVIEW_DEADLINE_EXPIRED"


@pytest.mark.parametrize(
    "reason",
    [
        "MODEL_TIMEOUT",
        "BUDGET_EXHAUSTED",
        "MODEL_UNAVAILABLE",
        "PUBLICATION_UNAVAILABLE",
        "DEADLINE_EXPIRED",
    ],
)
def test_known_failure_skips_signal_without_wait_or_retry(case, reason):
    case["advisory"] = {"status": "FALLBACK", "reason": reason}
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_RESULT_UNAVAILABLE"


def test_result_arriving_at_fixed_deadline_cannot_reopen_signal(case):
    case["now_ms"] = case["deadline_ms"]
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_REVIEW_DEADLINE_EXPIRED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "jev-latest"),
        ("prompt_version", "unverified-next-version"),
        ("schema", "unverified-next-schema"),
        ("request_key", "0" * 64),
        ("observed_at_ms", 100001),
        ("produced_at_ms", 100301),
        ("produced_at_ms", 99999),
        ("expires_at_ms", 100300),
        ("expires_at_ms", 130201),
        ("expires_at_ms", True),
        ("execution_authorized", True),
        ("risk_override", True),
        ("decision_effect", "OPEN"),
        ("probability_semantics", "TRADE_WIN_PROBABILITY"),
        ("usage", {"input_tokens": 8193, "output_tokens": 1}),
        ("hypotheses", {"trend_supported": "1", "chase_risk": "0"}),
        ("status", "APPROVED"),
    ],
)
def test_wrong_or_stale_proof_never_passes(case, field, value):
    case["advisory"][field] = value
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_RESULT_INVALID"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", "1.01", True, [], None])
def test_invalid_model_number_skips(case, value):
    case["advisory"]["hypotheses"]["trend_supported"] = value
    rejected(evaluate_entry_gate(**case))


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "ETHUSDT"),
        ("direction", "SHORT"),
        ("price", "101"),
        ("observed_at_ms", 100001),
        ("timeframe", "1h"),
    ],
)
def test_public_snapshot_cannot_be_substituted(case, field, value):
    case["public_features"][field] = value
    rejected(evaluate_entry_gate(**case))


def test_explicit_candidate_symbol_cannot_be_changed(case):
    case["symbol"] = "ETHUSDT"
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_CANDIDATE_BINDING_MISMATCH"


def test_short_direction_requires_short_bound_model_request(case):
    case["decision"] = replace(case["decision"], side="SELL")
    rejected(evaluate_entry_gate(**case))
    case["public_features"]["direction"] = "SHORT"
    rejected(evaluate_entry_gate(**case))
    prepared = prepare_request(case["public_features"], now_ms=100100, max_age_ms=60000)
    case["advisory"]["request_key"] = prepared["request_key"]
    result = evaluate_entry_gate(**case)
    assert result.status == "PASS" and result.decision.side == "SELL"


@pytest.mark.parametrize(
    "field,value",
    [
        ("now_ms", 100099),
        ("now_ms", True),
        ("now_ms", 100300.0),
        ("started_at_ms", 100301),
        ("started_at_ms", 99999),
        ("deadline_ms", 100100),
        ("deadline_ms", 105101),
        ("signal_expires_at_ms", 105099),
        ("signal_expires_at_ms", 2**53),
    ],
)
def test_invalid_timing_cannot_extend_wait(case, field, value):
    case[field] = value
    rejected(evaluate_entry_gate(**case))


def test_deadline_cannot_outlive_market_snapshot(case):
    case["policy"] = replace(case["policy"], max_age_ms=1000, cache_ttl_ms=1000)
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_FIXED_DEADLINE_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"admission_authorized": False, "execution_authorized": False},
        {"admission_authorized": True, "execution_authorized": True},
    ],
)
def test_non_admission_open_cannot_use_second_gate(case, payload):
    case["decision"] = replace(case["decision"], features_json=json.dumps(payload))
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert result.reason == "JEV_ORIGINAL_ADMISSION_INVALID"


def test_strategy_contract_already_forbids_non_object_features(case):
    with pytest.raises(TypeError, match="expected JSON object"):
        replace(case["decision"], features_json="[]")


@pytest.mark.parametrize(
    "advisory",
    [
        True,
        [],
        "secret-provider-text",
        {"status": "FALLBACK", "reason": "secret-provider-text"},
        {"status": "FALLBACK", "reason": []},
        {"status": "OFF"},
    ],
)
def test_malformed_or_disabled_results_never_pass_or_leak_raw_text(case, advisory):
    case["advisory"] = advisory
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert "secret-provider-text" not in result.evidence_json


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"min_trend_support": 0.6},
        {"min_trend_support": "NaN"},
        {"min_trend_support": "-0.1"},
        {"max_chase_risk": "1.1"},
        {"max_wait_ms": True},
        {"max_wait_ms": 30001},
        {"max_age_ms": 0},
        {"cache_ttl_ms": 60001},
    ],
)
def test_policy_rejects_invalid_unbounded_or_ambiguous_values(change):
    with pytest.raises(ValueError):
        JevEntryGatePolicy(**change)


def test_policy_thresholds_normalize_but_do_not_accept_float_shortcuts():
    assert JevEntryGatePolicy(min_trend_support="0.600").min_trend_support == "0.6"


def test_proof_does_not_accept_extra_provider_text(case):
    case["advisory"]["explanation"] = "secret-provider-text"
    result = evaluate_entry_gate(**case)
    rejected(result)
    assert "secret-provider-text" not in result.evidence_json


def test_original_model_record_is_never_modified_for_gate_decision(case):
    case["advisory"]["hypotheses"]["chase_risk"] = "1"
    original = deepcopy(case["advisory"])
    rejected(evaluate_entry_gate(**case), status="VETO")
    assert case["advisory"] == original
    assert case["advisory"]["decision_effect"] == "NONE"


def test_wrong_decision_or_policy_types_fail_explicitly(case):
    with pytest.raises(TypeError):
        evaluate_entry_gate(**{**case, "decision": object()})
    with pytest.raises(TypeError):
        evaluate_entry_gate(**{**case, "policy": {"enabled": True}})
