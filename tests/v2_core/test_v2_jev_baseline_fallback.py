"""Explicit availability fallback cannot erase risk or a real model veto."""

import json
from dataclasses import asdict, replace

import pytest
from test_v2_jev_entry_context import (
    POLICY,
    SCOPE,
    binding,
    claim,
    model_response,
    publish,
    refresh,
)
from test_v2_jev_entry_context import (
    case as context_case_fixture,
)
from test_v2_jev_entry_gate import case as gate_case_fixture

from services import v2_jev_coordinator as coordination
from services import v2_jev_entry_context as context_module
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from v2_core.evidence import canonical
from v2_core.jev_advisory import (
    AdvisoryPolicy,
    JevAdvisory,
    JevProviderRequestRejected,
    JevProviderUnavailable,
)
from v2_core.jev_entry_gate import BASELINE_FALLBACK_CAUSES, evaluate_entry_gate
from v2_core.state import StateSnapshot
from v2_core.strategy import StrategyDecision

gate_case = gate_case_fixture
context_case = context_case_fixture


def enable_baseline(case):
    case.service = context_module.JevEntryContext(
        case.db.connect,
        case.reference,
        scope=SCOPE,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )


def baseline_gate(case):
    case["policy"] = replace(case["policy"], unavailable_action="BASELINE")
    case["baseline_valid_until_ms"] = case["now_ms"] + 1000
    return case


@pytest.mark.parametrize(
    "reason", sorted(BASELINE_FALLBACK_CAUSES - {"MODEL_QUEUE_WAIT_EXHAUSTED"})
)
def test_only_known_availability_can_fallback_preserving_terms(gate_case, reason):
    baseline_gate(gate_case)
    original = gate_case["decision"]
    gate_case["advisory"] = {"status": "FALLBACK", "reason": reason}
    result = evaluate_entry_gate(**gate_case)
    assert result.status == "FALLBACK" and result.reason == "JEV_BASELINE_FALLBACK"
    assert result.decision is original
    evidence = json.loads(result.evidence_json)
    assert evidence["fallback_cause"] == reason
    assert evidence["hypotheses"] is None
    assert evidence["valid_until_ms"] == gate_case["baseline_valid_until_ms"]
    assert (
        evidence["execution_authorized"] is False and evidence["risk_override"] is False
    )


@pytest.mark.parametrize(
    "reason",
    [
        "MODEL_RESPONSE_INVALID",
        "MODEL_UNKNOWN_FAILURE",
        "TOKEN_RESERVATION_EXCEEDED",
        "COORDINATOR_UNAVAILABLE",
        "PUBLICATION_UNAVAILABLE",
        "CACHE_UNAVAILABLE",
        "CONTROL_UNAVAILABLE",
        "CONTROL_STALE",
        "REFERENCE_UNAVAILABLE",
        "DISABLED",
        "STOPPED",
        "CLOCK_INVALID",
        "INVALID_OR_STALE_MARKET",
        "UNRECOGNIZED",
    ],
)
def test_unknown_corruption_control_failure_are_never_baseline(gate_case, reason):
    baseline_gate(gate_case)
    gate_case["advisory"] = {"status": "FALLBACK", "reason": reason}
    result = evaluate_entry_gate(**gate_case)
    assert result.status in {"SKIP", "PENDING"}
    assert result.decision is None or result.decision.action != "OPEN"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("trend_supported", "0.5", "JEV_TREND_UNSUPPORTED"),
        ("chase_risk", "0.8", "JEV_CHASE_RISK_HIGH"),
    ],
)
def test_available_real_veto_does_not_fallback(gate_case, field, value, reason):
    baseline_gate(gate_case)
    gate_case["advisory"]["hypotheses"][field] = value
    result = evaluate_entry_gate(**gate_case)
    assert result.status == "VETO" and result.reason == reason


@pytest.mark.parametrize("advisory", [None, {"status": "FALLBACK", "reason": "BUSY"}])
def test_missing_work_wait_is_bounded_not_reset(gate_case, advisory):
    baseline_gate(gate_case)
    gate_case["advisory"] = advisory
    assert evaluate_entry_gate(**gate_case).status == "PENDING"
    gate_case["now_ms"] = gate_case["started_at_ms"] + 1500
    gate_case["baseline_valid_until_ms"] = gate_case["now_ms"] + 1000
    result = evaluate_entry_gate(**gate_case)
    assert result.status == "FALLBACK"
    assert (
        json.loads(result.evidence_json)["fallback_cause"]
        == "MODEL_QUEUE_WAIT_EXHAUSTED"
    )


@pytest.mark.parametrize("ttl", [None, True, 100300, -1, "100400"])
def test_missing_or_expired_fresh_context_never_fallback(gate_case, ttl):
    baseline_gate(gate_case)
    gate_case.update(
        advisory={"status": "FALLBACK", "reason": "BUDGET_EXHAUSTED"},
        baseline_valid_until_ms=ttl,
    )
    assert evaluate_entry_gate(**gate_case).status == "SKIP"


def test_expired_signal_never_resurrected_by_availability(gate_case):
    baseline_gate(gate_case)
    gate_case.update(
        advisory=None,
        now_ms=gate_case["signal_expires_at_ms"],
        baseline_valid_until_ms=130000,
    )
    assert evaluate_entry_gate(**gate_case).status == "SKIP"


def test_forged_availability_extras_never_baseline(gate_case):
    baseline_gate(gate_case)
    gate_case["advisory"] = {
        "status": "FALLBACK",
        "reason": "MODEL_TIMEOUT",
        "risk_override": True,
    }
    assert evaluate_entry_gate(**gate_case).status == "SKIP"


@pytest.mark.parametrize(
    "window,expected",
    [("minute", "REQUEST_BUDGET_EXHAUSTED"), ("day", "TOKEN_BUDGET_EXHAUSTED")],
)
def test_no_jev_request_or_tokens_needed_when_budget_exhausted(
    context_case, window, expected
):
    case = context_case
    enable_baseline(case)
    minute, day = coordination._window_starts(case.db.now)
    namespace, at, kind, limit = (
        (coordination.MINUTE_NAMESPACE, minute, "minute", 5)
        if window == "minute"
        else (coordination.DAY_NAMESPACE, day, "utc8-day", 500000)
    )
    key = coordination._budget_key(namespace, at)
    payload = coordination._budget(None, kind=kind, start=at, limit=limit)
    payload["used"] = limit
    saved = StateSnapshot(1, canonical(payload), False)
    case.db.rows[key.identity] = saved
    result = case.service.assess(**case.args)
    assert result.status == "FALLBACK" and result.decision is case.args["decision"]
    assert json.loads(result.evidence_json)["fallback_cause"] == expected
    assert not case.queued and case.db.rows[key.identity] == saved
    assert binding(case)[2]["started_at_ms"] == case.db.now


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("MODEL_TIMEOUT", "FALLBACK"),
        ("MODEL_UNAVAILABLE", "FALLBACK"),
        ("PROVIDER_REQUEST_REJECTED", "FALLBACK"),
        ("MODEL_RESPONSE_INVALID", "SKIP"),
        ("MODEL_UNKNOWN_FAILURE", "SKIP"),
    ],
)
def test_durable_provider_failure_classification_survives_restart(
    context_case, failure, expected
):
    case = context_case
    enable_baseline(case)
    assert case.service.assess(**case.args).status == "PENDING"
    publish(case, failure=failure)
    case.db.now += 2000
    refresh(case.args, case.db.now)
    enable_baseline(case)
    assert case.service.assess(**case.args).status == expected
    assert len(case.queued) == 1


def test_baseline_still_preserves_hard_risk_rejection(context_case):
    case = context_case
    enable_baseline(case)
    decision = StrategyDecision("IGNORED", "PORTFOLIO_RISK_LIMIT")
    case.args["decision"] = decision
    result = case.service.assess(**case.args)
    assert result.status == "VETO" and result.decision is decision and not case.queued


@pytest.mark.parametrize("corruption", ["scope", "expired", "bound", "db"])
def test_corrupt_or_stale_fresh_admission_never_fallback(context_case, corruption):
    case = context_case
    enable_baseline(case)
    assert case.service.assess(**case.args).status == "PENDING"
    case.db.now += 2000
    refresh(case.args, case.db.now)
    if corruption == "scope":
        case.args["context"]["account_scope"]["account_id"] = "wrong"
    elif corruption == "expired":
        case.args["context"]["valid_until_ms"] = case.db.now
    elif corruption == "bound":
        key, _, payload = binding(case)
        payload["request_key"] = "0" * 64
        case.db.rows[key.identity] = StateSnapshot(1, canonical(payload), False)
    else:
        case.service.connect = lambda: (_ for _ in ()).throw(RuntimeError("SECRET"))
    result = case.service.assess(**case.args)
    assert result.status == "SKIP" and "SECRET" not in result.evidence_json


def test_disabled_provider_is_not_implicitly_bypassed(context_case):
    case = context_case
    enable_baseline(case)
    case.db.rows[CONTROL_KEY.identity] = StateSnapshot(
        2, canonical(asdict(AdvisoryPolicy(enabled=False))), False
    )
    assert case.service.assess(**case.args).status == "SKIP"
    assert not case.queued


@pytest.mark.parametrize(
    "kind",
    [
        "invalid",
        "unknown",
        "transport",
        "late_veto",
        "excess_tokens",
        "provider_rejected",
    ],
)
def test_late_invalid_or_unknown_provider_response_not_relabelled_as_timeout(
    context_case, kind
):
    case = context_case
    enable_baseline(case)
    case.service.assess(**case.args)
    _, _, bound = binding(case)
    coordinator = coordination.JevCoordinator(
        case.db.connect, scope=PUBLIC_SCOPE, enabled=True
    )

    def query(request, timeout_ms):
        case.db.now += timeout_ms
        if kind == "invalid":
            return {"SECRET": "bad response"}
        if kind == "unknown":
            raise RuntimeError("SECRET")
        if kind == "provider_rejected":
            raise JevProviderRequestRejected()
        if kind in {"late_veto", "excess_tokens"}:
            result = model_response(trend="0.1")
            if kind == "excess_tokens":
                result["usage"]["input_tokens"] = 9000
            return result
        raise JevProviderUnavailable("SECRET")

    result = JevAdvisory(
        coordinator,
        query,
        policy=AdvisoryPolicy(enabled=True),
        clock_ms=lambda: case.db.now,
    ).evaluate(bound["public_features"])
    expected = {
        "invalid": "MODEL_RESPONSE_INVALID",
        "unknown": "MODEL_UNKNOWN_FAILURE",
        "transport": "DEADLINE_EXPIRED",
        "late_veto": "MODEL_LATE_RESPONSE",
        "excess_tokens": "TOKEN_RESERVATION_EXCEEDED",
        "provider_rejected": "PROVIDER_REQUEST_REJECTED",
    }[kind]
    assert result["reason"] == expected and "SECRET" not in json.dumps(result)
    refresh(case.args, case.db.now)
    assert case.service.assess(**case.args).status == (
        "FALLBACK" if kind in {"transport", "provider_rejected"} else "SKIP"
    )


def test_bounded_wait_wakes_for_baseline_without_resetting_binding(context_case):
    case = context_case
    enable_baseline(case)
    case.service.assess(**case.args)
    _, original, _ = binding(case)
    case.db.now += 1500
    assert case.service.wait_pending(
        signal_id=case.args["signal_id"],
        consumer=case.args["consumer"],
        max_wait_ms=100,
    )
    assert binding(case)[1] == original


def test_ambiguous_expired_claim_is_not_a_proven_model_timeout(context_case):
    case = context_case
    enable_baseline(case)
    case.service.assess(**case.args)
    claim(case)
    case.db.now += 2000
    refresh(case.args, case.db.now)
    result = case.service.assess(**case.args)
    assert result.status == "SKIP" and result.reason == "JEV_RESULT_UNAVAILABLE"


def test_corrupt_claim_deadline_cannot_fabricate_timeout_fallback(context_case):
    case = context_case
    enable_baseline(case)
    case.service.assess(**case.args)
    coordinator, prepared, _ = claim(case)
    key = coordinator.request_state_key(prepared["request_key"])
    saved = case.db.rows[key.identity]
    payload = json.loads(saved.payload_json)
    payload["deadline_ms"] = 0
    case.db.rows[key.identity] = StateSnapshot(saved.version, canonical(payload), False)
    case.db.now += 2000
    refresh(case.args, case.db.now)
    assert case.service.assess(**case.args).status == "SKIP"
