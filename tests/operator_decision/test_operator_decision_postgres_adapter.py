"""Unit boundaries for the injected operator-decision PostgreSQL adapter."""
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import DelayedAction, DelayedEventKind
from operator_decision import (
    CreateDecisionCode,
    DecisionItem,
    DecisionSeverity,
    DecisionStatus,
    NotificationCasCode,
    NotificationClaimCode,
    NotificationOutboxItem,
    PostgresOperatorDecisionStore,
)
from position_identity.slot import ExchangePositionKey


def _pair():
    decision = DecisionItem.open(
        decision_id=str(uuid4()), event_id=str(uuid4()),
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="adapter-unit", environment="SANDBOX",
            symbol="ETHUSDT",
        ),
        event_kind=DelayedEventKind.UNKNOWN_MUTATION,
        severity=DecisionSeverity.HIGH,
        trigger_context={}, current_context={},
        trigger_context_digest="a" * 64, current_context_digest="a" * 64,
        fallback_action=DelayedAction.QUERY_AND_RECONCILE,
        fallback_at=20, now=10,
    )
    notification = NotificationOutboxItem.pending(
        notification_id=str(uuid4()), decision_id=decision.decision_id,
        dedupe_key=f"decision:{decision.decision_id}", payload={}, now=10,
    )
    return decision, notification


@contextmanager
def _unavailable():
    raise OSError("database unavailable")
    yield  # pragma: no cover


def test_adapter_requires_injected_factory_and_fails_closed():
    with pytest.raises(TypeError, match="connection_factory"):
        PostgresOperatorDecisionStore(None)
    store = PostgresOperatorDecisionStore(_unavailable)
    decision, notification = _pair()
    assert store.create_with_notification(
        decision, notification).code is CreateDecisionCode.UNKNOWN
    assert store.read_decision(decision.decision_id).code.value == "UNAVAILABLE"
    assert store.list_inbox().code.value == "UNAVAILABLE"
    assert store.claim_notifications(
        "worker", 10).code is NotificationClaimCode.UNKNOWN


def test_create_rejects_mismatched_or_advanced_records_before_io():
    store = PostgresOperatorDecisionStore(_unavailable)
    decision, notification = _pair()
    with pytest.raises(ValueError, match="decision_id mismatch"):
        store.create_with_notification(
            decision, replace(notification, decision_id=str(uuid4())))
    acknowledged = decision.transition(
        status=DecisionStatus.ACKNOWLEDGED, now=11)
    with pytest.raises(ValueError, match="OPEN version-1"):
        store.create_with_notification(acknowledged, notification)


def test_decision_cas_rejects_version_identity_and_status_regression():
    store = PostgresOperatorDecisionStore(_unavailable)
    decision, _ = _pair()
    with pytest.raises(ValueError, match="advance exactly once"):
        store.compare_and_swap_decision(decision, decision)
    wrong = replace(decision, decision_id=str(uuid4()), version=2)
    with pytest.raises(ValueError, match="decision_id mismatch"):
        store.compare_and_swap_decision(decision, wrong)
    acknowledged = decision.transition(
        status=DecisionStatus.ACKNOWLEDGED, now=11)
    regressed = replace(acknowledged, status=DecisionStatus.OPEN, version=3,
                        updated_at=12)
    with pytest.raises(ValueError, match="status transition is illegal"):
        store.compare_and_swap_decision(acknowledged, regressed)
    backwards = replace(
        acknowledged, version=3, current_context_digest="b" * 64,
        updated_at=10,
    )
    with pytest.raises(ValueError, match="time cannot move backwards"):
        store.compare_and_swap_decision(acknowledged, backwards)


def test_notification_cas_requires_claim_and_preserves_immutable_fields():
    store = PostgresOperatorDecisionStore(_unavailable)
    _, pending = _pair()
    with pytest.raises(ValueError, match="requires CLAIMED"):
        store.compare_and_swap_notification(pending, pending)
    claimed = pending.claim(owner_token="worker", lease_expires_at=20, now=11)
    delivered = claimed.delivered(now=12)
    mutated = replace(delivered, payload_json='{"changed":true}')
    with pytest.raises(ValueError, match="immutable notification"):
        store.compare_and_swap_notification(claimed, mutated)
    backwards = replace(delivered, updated_at=10.5, delivered_at=10.5)
    with pytest.raises(ValueError, match="time cannot move backwards"):
        store.compare_and_swap_notification(claimed, backwards)
    result = store.compare_and_swap_notification(claimed, delivered)
    assert result.code is NotificationCasCode.UNKNOWN


def test_claim_input_bounds_are_validated_before_io():
    store = PostgresOperatorDecisionStore(_unavailable)
    with pytest.raises(ValueError, match="owner_token"):
        store.claim_notifications(" worker ", 10)
    with pytest.raises(ValueError, match="lease_seconds"):
        store.claim_notifications("worker", float("nan"))
    with pytest.raises(ValueError, match="limit"):
        store.claim_notifications("worker", 10, 0)
