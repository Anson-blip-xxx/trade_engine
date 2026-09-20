from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import DelayedAction, DelayedEventKind
from operator_decision import (
    DecisionItem,
    DecisionResolution,
    DecisionSeverity,
    DecisionStatus,
    NotificationOutboxItem,
    NotificationStatus,
)
from position_identity.slot import ExchangePositionKey

KEY = ExchangePositionKey.one_way(
    account_principal_id="decision-center", environment="SANDBOX",
    symbol="BTCUSDT",
)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _decision():
    return DecisionItem.open(
        decision_id=str(uuid4()), event_id=str(uuid4()),
        operation_id=str(uuid4()), exchange_position_key=KEY,
        event_kind=DelayedEventKind.UNKNOWN_MUTATION,
        severity=DecisionSeverity.HIGH,
        trigger_context={"stage": "UNKNOWN", "quantity": "1"},
        current_context={"stage": "UNKNOWN", "quantity": "1"},
        trigger_context_digest=DIGEST_A,
        current_context_digest=DIGEST_A,
        fallback_action=DelayedAction.QUERY_AND_RECONCILE,
        fallback_at=30, approval_expires_at=20, now=1,
    )


def test_open_decision_is_canonical_and_refreshes_current_version():
    item = _decision()
    assert item.status is DecisionStatus.OPEN
    assert item.version == 1
    assert item.trigger_context_json == '{"quantity":"1","stage":"UNKNOWN"}'
    refreshed = item.refresh_current(
        current_context={"quantity": "2", "stage": "UNKNOWN"},
        current_context_digest=DIGEST_B,
        now=2,
    )
    assert refreshed.version == 2
    assert refreshed.current_context_digest == DIGEST_B
    assert item.current_context_digest == DIGEST_A


def test_acknowledge_then_resolve_is_versioned_and_terminal():
    item = _decision().transition(
        status=DecisionStatus.ACKNOWLEDGED, now=2,
        assigned_to="operator-a",
    )
    assert item.assigned_to == "operator-a"
    resolved = item.transition(
        status=DecisionStatus.RESOLVED, now=3,
        resolution=DecisionResolution.QUARANTINE,
        resolution_detail={"reason": "evidence remains ambiguous"},
    )
    assert resolved.version == 3
    assert resolved.resolution is DecisionResolution.QUARANTINE
    assert resolved.resolution_json == '{"reason":"evidence remains ambiguous"}'
    with pytest.raises(ValueError, match="illegal decision transition"):
        resolved.transition(status=DecisionStatus.EXPIRED, now=4)
    with pytest.raises(ValueError, match="terminal decision"):
        resolved.refresh_current(
            current_context={}, current_context_digest=DIGEST_B, now=4)


def test_resolution_and_detail_are_only_valid_on_resolved_state():
    item = _decision()
    with pytest.raises(ValueError, match="requires resolution"):
        replace(item, status=DecisionStatus.RESOLVED)
    with pytest.raises(ValueError, match="only RESOLVED"):
        replace(item, resolution=DecisionResolution.REJECT)
    with pytest.raises(ValueError, match="resolution detail"):
        replace(item, resolution_json='{"reason":"hidden"}')
    with pytest.raises(ValueError, match="requires resolution"):
        item.transition(status=DecisionStatus.RESOLVED, now=2)


def test_decision_rejects_bad_context_digest_time_and_json():
    item = _decision()
    with pytest.raises(ValueError, match="SHA-256"):
        replace(item, current_context_digest="bad")
    with pytest.raises(TypeError, match="JSON object"):
        replace(item, current_context_json="[]")
    with pytest.raises(ValueError, match="fallback_at"):
        replace(item, fallback_at=0)
    with pytest.raises(ValueError, match="backwards"):
        item.refresh_current(
            current_context={}, current_context_digest=DIGEST_A, now=0)


def _notification():
    return NotificationOutboxItem.pending(
        notification_id=str(uuid4()), decision_id=_decision().decision_id,
        dedupe_key="decision:telegram:v1", payload={"decision_id": "safe"},
        now=1,
    )


def test_notification_claim_retry_reclaim_and_delivery_lifecycle():
    pending = _notification()
    assert pending.status is NotificationStatus.PENDING
    assert pending.attempt_count == 0
    claimed = pending.claim(owner_token="worker-a", lease_expires_at=5, now=1)
    assert claimed.status is NotificationStatus.CLAIMED
    assert claimed.attempt_count == 1
    retry = claimed.retry(next_attempt_at=10, last_error="tg unavailable", now=2)
    assert retry.status is NotificationStatus.RETRY_WAIT
    assert retry.owner_token is None
    with pytest.raises(ValueError, match="not due"):
        retry.claim(owner_token="worker-b", lease_expires_at=12, now=9)
    reclaimed = retry.claim(
        owner_token="worker-b", lease_expires_at=15, now=10)
    delivered = reclaimed.delivered(now=11)
    assert delivered.status is NotificationStatus.DELIVERED
    assert delivered.attempt_count == 2
    assert delivered.delivered_at == 11
    with pytest.raises(ValueError, match="pending/retry/expired-claim"):
        delivered.claim(owner_token="worker-c", lease_expires_at=20, now=12)


def test_notification_expired_claim_can_be_taken_over_but_live_lease_cannot():
    claimed = _notification().claim(
        owner_token="failed-worker", lease_expires_at=5, now=1)
    with pytest.raises(ValueError, match="still active"):
        claimed.claim(owner_token="other-worker", lease_expires_at=6, now=4)
    reclaimed = claimed.claim(
        owner_token="other-worker", lease_expires_at=10, now=5)
    assert reclaimed.owner_token == "other-worker"
    assert reclaimed.attempt_count == 2
    assert reclaimed.version == claimed.version + 1


def test_notification_dead_letter_requires_claim_and_error():
    pending = _notification()
    with pytest.raises(ValueError, match="must be CLAIMED"):
        pending.dead_letter(last_error="exhausted", now=2)
    claimed = pending.claim(owner_token="worker", lease_expires_at=5, now=1)
    dead = claimed.dead_letter(last_error="exhausted", now=2)
    assert dead.status is NotificationStatus.DEAD_LETTER
    assert dead.last_error == "exhausted"


def test_notification_shape_invariants_fail_closed():
    pending = _notification()
    with pytest.raises(ValueError, match="cannot have attempts"):
        replace(pending, attempt_count=1)
    with pytest.raises(ValueError, match="only CLAIMED"):
        replace(pending, owner_token="worker", lease_expires_at=5)
    with pytest.raises(ValueError, match="within record lifetime"):
        replace(
            pending, status=NotificationStatus.DELIVERED,
            attempt_count=1, delivered_at=2,
        )
    with pytest.raises(ValueError, match="future next_attempt_at"):
        replace(
            pending, status=NotificationStatus.RETRY_WAIT,
            attempt_count=1, next_attempt_at=1, last_error="failed",
        )


def test_notification_requires_live_lease_and_monotonic_retry_time():
    pending = _notification()
    with pytest.raises(ValueError, match="greater than now"):
        pending.claim(owner_token="worker", lease_expires_at=1, now=1)
    claimed = pending.claim(owner_token="worker", lease_expires_at=5, now=1)
    with pytest.raises(ValueError, match="greater than now"):
        claimed.retry(next_attempt_at=2, last_error="failed", now=2)
    with pytest.raises(ValueError, match="backwards"):
        claimed.delivered(now=0)
