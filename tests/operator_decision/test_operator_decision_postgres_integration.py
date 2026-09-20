"""Opt-in operator-decision QA against an isolated PostgreSQL database."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from operation_journal import DelayedAction, DelayedEventKind
from operator_decision import (
    CreateDecisionCode,
    DecisionCasCode,
    DecisionItem,
    DecisionReadCode,
    DecisionSeverity,
    DecisionStatus,
    NotificationCasCode,
    NotificationClaimCode,
    NotificationOutboxItem,
    PostgresOperatorDecisionStore,
)
from position_identity.slot import ExchangePositionKey

DSN = os.environ.get("OPERATOR_DECISION_TEST_DSN")
ISOLATED = os.environ.get("OPERATOR_DECISION_TEST_CONFIRM_ISOLATED") == "YES"
pytestmark = pytest.mark.skipif(
    not DSN or not ISOLATED,
    reason="requires explicit DSN and isolated-database confirmation",
)


def _pair(*, dedupe_key=None):
    now = round(time.time(), 6)
    decision = DecisionItem.open(
        decision_id=str(uuid4()), event_id=str(uuid4()),
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="decision-pg-test", environment="SANDBOX",
            symbol="BTCUSDT",
        ),
        event_kind=DelayedEventKind.UNKNOWN_MUTATION,
        severity=DecisionSeverity.HIGH,
        trigger_context={"stage": "UNKNOWN"},
        current_context={"stage": "UNKNOWN"},
        trigger_context_digest="a" * 64,
        current_context_digest="a" * 64,
        fallback_action=DelayedAction.QUERY_AND_RECONCILE,
        fallback_at=now + 300, approval_expires_at=now + 120, now=now,
    )
    notification = NotificationOutboxItem.pending(
        notification_id=str(uuid4()), decision_id=decision.decision_id,
        dedupe_key=dedupe_key or f"decision:{decision.decision_id}:telegram:v1",
        payload={"decision_id": decision.decision_id}, now=now,
    )
    return decision, notification


@pytest.fixture
def store():
    import psycopg
    from psycopg import sql

    schema_name = f"operator_decision_test_{uuid4().hex}"
    schema = (
        Path(__file__).resolve().parents[2] /
        "db/postgres_operator_decision_schema.sql"
    ).read_text()
    with psycopg.connect(DSN) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(
            sql.Identifier(schema_name)))
        conn.execute(sql.SQL("SET search_path TO {}").format(
            sql.Identifier(schema_name)))
        conn.execute(schema)

    @contextmanager
    def connection():
        with psycopg.connect(DSN) as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(
                sql.Identifier(schema_name)))
            yield conn

    yield PostgresOperatorDecisionStore(connection)
    with psycopg.connect(DSN) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(schema_name)))


def test_real_postgres_atomic_create_read_list_and_idempotency(store):
    decision, notification = _pair()
    created = store.create_with_notification(decision, notification)
    assert created.code is CreateDecisionCode.CREATED
    assert created.decision == decision
    assert created.notification == notification
    duplicate = store.create_with_notification(decision, notification)
    assert duplicate.code is CreateDecisionCode.ALREADY_EXISTS
    assert store.read_decision(decision.decision_id).decision == decision
    assert store.list_inbox().decisions == (decision,)


def test_real_postgres_decision_cas_has_one_winner(store):
    decision, notification = _pair()
    assert store.create_with_notification(
        decision, notification).code is CreateDecisionCode.CREATED

    def acknowledge(operator):
        desired = decision.transition(
            status=DecisionStatus.ACKNOWLEDGED,
            assigned_to=operator, now=decision.updated_at + 0.000001,
        )
        return store.compare_and_swap_decision(decision, desired)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acknowledge, ("operator-a", "operator-b")))
    assert [result.code for result in results].count(DecisionCasCode.APPLIED) == 1
    assert [result.code for result in results].count(
        DecisionCasCode.STALE_VERSION) == 1


def test_real_postgres_notification_claim_and_delivery_are_fenced(store):
    decision, notification = _pair()
    store.create_with_notification(decision, notification)

    def claim(owner):
        return store.claim_notifications(owner, 10, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))
    assert [result.code for result in results].count(
        NotificationClaimCode.CLAIMED) == 1
    assert [result.code for result in results].count(
        NotificationClaimCode.EMPTY) == 1
    claimed = next(
        result.notifications[0] for result in results
        if result.code is NotificationClaimCode.CLAIMED
    )
    desired = claimed.delivered(now=claimed.updated_at + 0.000001)
    applied = store.compare_and_swap_notification(claimed, desired)
    assert applied.code is NotificationCasCode.APPLIED
    assert applied.notification.status.value == "DELIVERED"
    assert store.compare_and_swap_notification(
        claimed, desired).code is NotificationCasCode.STALE_VERSION


def test_real_postgres_expired_notification_claim_is_recovered(store):
    decision, notification = _pair()
    store.create_with_notification(decision, notification)
    first = store.claim_notifications("failed-worker", 0.05, 1)
    assert first.code is NotificationClaimCode.CLAIMED
    time.sleep(0.1)
    second = store.claim_notifications("recovery-worker", 10, 1)
    assert second.code is NotificationClaimCode.CLAIMED
    assert second.notifications[0].owner_token == "recovery-worker"
    assert second.notifications[0].attempt_count == 2


def test_real_postgres_notification_conflict_rolls_back_decision(store):
    dedupe_key = f"shared:{uuid4()}"
    first_decision, first_notification = _pair(dedupe_key=dedupe_key)
    second_decision, second_notification = _pair(dedupe_key=dedupe_key)
    assert store.create_with_notification(
        first_decision, first_notification).code is CreateDecisionCode.CREATED
    assert store.create_with_notification(
        second_decision, second_notification).code is CreateDecisionCode.UNKNOWN
    assert store.read_decision(
        second_decision.decision_id).code is DecisionReadCode.NOT_FOUND
