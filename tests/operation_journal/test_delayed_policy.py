from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    ApprovalBinding,
    ApprovalCode,
    AutonomySafetyPolicy,
    DelayedAction,
    DelayedEvent,
    DelayedEventKind,
    OperationType,
    OperatorAction,
    OperatorApproval,
    PositionDirection,
    canonical_quantity,
    decide_delayed_event,
    validate_operator_approval,
)
from position_identity.slot import ExchangePositionKey

DIGEST = "a" * 64
KEY = ExchangePositionKey.one_way(
    account_principal_id="delayed-policy", environment="SANDBOX",
    symbol="BTCUSDT",
)


@pytest.fixture
def policy():
    return AutonomySafetyPolicy(
        short_retry_seconds=30,
        isolation_seconds=300,
        emergency_seconds=900,
        approval_wait_seconds=600,
    )


def _event(kind, occurred_at=100):
    return DelayedEvent(str(uuid4()), kind, occurred_at, DIGEST)


def _decide(kind, age, policy, *, matches=True):
    return decide_delayed_event(
        _event(kind), now=100 + age, policy=policy,
        current_context_digest=(DIGEST if matches else "b" * 64),
    )


def test_changed_context_always_rebuilds_and_never_rebinds_old_work(policy):
    for kind in DelayedEventKind:
        decision = _decide(kind, 1, policy, matches=False)
        assert decision.action is DelayedAction.REBUILD_DECISION
        assert decision.requires_new_event
        assert not decision.exchange_mutation_candidate
        assert not decision.may_increase_risk


def test_risk_increase_revalidates_briefly_then_expires_cancelled(policy):
    fresh = _decide(DelayedEventKind.RISK_INCREASING_REQUEST, 30, policy)
    assert fresh.action is DelayedAction.REVALIDATE_REQUEST
    assert fresh.requires_generation_revalidation
    assert not fresh.exchange_mutation_candidate

    expired = _decide(DelayedEventKind.RISK_INCREASING_REQUEST, 30.000001, policy)
    assert expired.action is DelayedAction.CANCEL_REQUEST
    assert expired.requires_new_event


def test_unknown_is_query_only_then_quarantined_and_never_retried(policy):
    fresh = _decide(DelayedEventKind.UNKNOWN_MUTATION, 30, policy)
    assert fresh.action is DelayedAction.QUERY_AND_RECONCILE
    old = _decide(DelayedEventKind.UNKNOWN_MUTATION, 301, policy)
    assert old.action is DelayedAction.QUARANTINE_AND_QUERY
    for decision in (fresh, old):
        assert decision.requires_generation_revalidation
        assert not decision.exchange_mutation_candidate
        assert not decision.may_increase_risk


def test_external_position_is_quarantined_without_native_adoption(policy):
    decision = _decide(DelayedEventKind.EXTERNAL_POSITION, 1, policy)
    assert decision.action is DelayedAction.QUARANTINE_AND_QUERY
    assert not decision.exchange_mutation_candidate


def test_unprotected_position_escalates_but_market_close_defaults_off(policy):
    short = _decide(DelayedEventKind.UNPROTECTED_POSITION, 30, policy)
    medium = _decide(DelayedEventKind.UNPROTECTED_POSITION, 31, policy)
    isolated = _decide(DelayedEventKind.UNPROTECTED_POSITION, 301, policy)
    long = _decide(DelayedEventKind.UNPROTECTED_POSITION, 901, policy)
    assert short.action is DelayedAction.RESTORE_PROTECTION
    assert medium.action is DelayedAction.PAUSE_OPENS_AND_RESTORE_PROTECTION
    assert isolated.action is DelayedAction.QUARANTINE_AND_RESTORE_PROTECTION
    assert long.action is DelayedAction.QUARANTINE_AND_RESTORE_PROTECTION
    for decision in (short, medium, isolated, long):
        assert decision.exchange_mutation_candidate
        assert decision.requires_generation_revalidation
        assert decision.requires_mutation_ownership
        assert not decision.may_increase_risk


def test_emergency_reduce_only_requires_explicit_policy_opt_in(policy):
    enabled = replace(policy, automatic_emergency_close=True)
    decision = _decide(
        DelayedEventKind.UNPROTECTED_POSITION, 901, enabled)
    assert decision.action is DelayedAction.EMERGENCY_REDUCE_ONLY
    assert decision.requires_mutation_ownership


def test_operator_wait_expires_into_fresh_decision(policy):
    waiting = _decide(DelayedEventKind.OPERATOR_APPROVAL, 600, policy)
    expired = _decide(DelayedEventKind.OPERATOR_APPROVAL, 600.000001, policy)
    assert waiting.action is DelayedAction.WAIT_FOR_APPROVAL
    assert expired.action is DelayedAction.REBUILD_DECISION
    assert expired.requires_new_event


def test_policy_and_event_validate_time_order_and_canonical_digest(policy):
    with pytest.raises(ValueError, match="short_retry_seconds"):
        replace(policy, isolation_seconds=10)
    with pytest.raises(TypeError, match="boolean"):
        replace(policy, automatic_emergency_close=1)
    with pytest.raises(ValueError, match="SHA-256"):
        DelayedEvent(str(uuid4()), DelayedEventKind.UNKNOWN_MUTATION, 1, "bad")
    with pytest.raises(ValueError, match="lowercase"):
        DelayedEvent(
            str(uuid4()), DelayedEventKind.UNKNOWN_MUTATION, 1, "A" * 64)
    with pytest.raises(ValueError, match="precede"):
        decide_delayed_event(
            _event(DelayedEventKind.UNKNOWN_MUTATION), now=99,
            policy=policy, current_context_digest=DIGEST,
        )


def _binding():
    return ApprovalBinding(
        operation_id=str(uuid4()), operation_type=OperationType.CLOSE_FULL,
        operation_version=3,
        exchange_position_key=KEY, episode_id=str(uuid4()),
        lifecycle_generation=4, protection_generation=2,
        authority_revision=5, desired_revision=6,
        policy_version="v2-approved-2026-09-20", evidence_observed_at=90,
        position_direction=PositionDirection.LONG, position_quantity="1.2500",
        evidence_digest=DIGEST, action=OperatorAction.RETRY_MUTATION,
    )


def test_operator_approval_is_exact_context_bound_and_expires():
    binding = _binding()
    approval = OperatorApproval(str(uuid4()), binding, 100, 200)
    assert validate_operator_approval(
        approval, expected=binding, now=100) is ApprovalCode.VALID
    assert validate_operator_approval(
        approval, expected=binding, now=199.999999) is ApprovalCode.VALID
    assert validate_operator_approval(
        approval, expected=binding, now=200) is ApprovalCode.EXPIRED
    assert validate_operator_approval(
        approval, expected=binding, now=99) is ApprovalCode.NOT_YET_VALID
    assert validate_operator_approval(
        approval, expected=replace(binding, authority_revision=6),
        now=150,
    ) is ApprovalCode.CONTEXT_MISMATCH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_version", 0),
        ("lifecycle_generation", True),
        ("authority_revision", -1),
        ("protection_generation", 0),
        ("desired_revision", 0),
    ],
)
def test_approval_binding_rejects_invalid_versions(field, value):
    with pytest.raises(ValueError, match=field):
        replace(_binding(), **{field: value})


def test_approval_binding_canonicalizes_and_validates_position_evidence():
    binding = _binding()
    assert binding.position_quantity == "1.25"
    with pytest.raises(ValueError, match="zero quantity"):
        replace(
            binding, position_direction=PositionDirection.FLAT,
            position_quantity="1",
        )
    with pytest.raises(ValueError, match="positive quantity"):
        replace(binding, position_quantity="0")
    with pytest.raises(ValueError, match="policy_version"):
        replace(binding, policy_version=" ")
    with pytest.raises(TypeError, match="operation_type"):
        replace(binding, operation_type="CLOSE_FULL")


def test_approval_requires_positive_window_and_typed_inputs():
    binding = _binding()
    with pytest.raises(ValueError, match="greater"):
        OperatorApproval(str(uuid4()), binding, 100, 100)
    with pytest.raises(ValueError, match="exchange evidence"):
        OperatorApproval(
            str(uuid4()), replace(binding, evidence_observed_at=101), 100, 200)
    with pytest.raises(TypeError, match="OperatorApproval"):
        validate_operator_approval(object(), expected=binding, now=100)
    with pytest.raises(TypeError, match="ApprovalBinding"):
        validate_operator_approval(
            OperatorApproval(str(uuid4()), binding, 100, 200),
            expected=object(), now=100,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2300", "1.23"), (0, "0"), ("0.000100", "0.0001")],
)
def test_quantity_canonicalization_for_evidence_digest(value, expected):
    assert canonical_quantity(value) == expected


@pytest.mark.parametrize("value", [True, "nan", "inf", -1])
def test_quantity_canonicalization_rejects_unsafe_values(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_quantity(value)
