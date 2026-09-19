"""Opt-in integration QA against an explicitly isolated PostgreSQL database."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from operation_journal import (
    CasCode,
    CreateCode,
    LeaseCode,
    OperationRecord,
    OperationStage,
    OperationType,
    PostgresOperationJournal,
)
from position_identity.slot import ExchangePositionKey

DSN = os.environ.get("OPERATION_JOURNAL_TEST_DSN")
ISOLATED = os.environ.get("OPERATION_JOURNAL_TEST_CONFIRM_ISOLATED") == "YES"
pytestmark = pytest.mark.skipif(
    not DSN or not ISOLATED,
    reason="requires explicit DSN and isolated-database confirmation",
)


def _operation():
    return OperationRecord.new(
        operation_id=str(uuid4()), operation_type=OperationType.OPEN,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="pg-integration", environment="SANDBOX",
            symbol="BTCUSDT",
        ),
        normalized_input={"quantity": "0.01"}, now=10.123456789,
        request_id=str(uuid4()),
    )


@pytest.fixture
def journal():
    import psycopg

    schema = (
        Path(__file__).resolve().parents[2] /
        "db/postgres_operation_journal_schema.sql"
    ).read_text()
    with psycopg.connect(DSN) as conn:
        conn.execute(schema)

    @contextmanager
    def connection():
        with psycopg.connect(DSN) as conn:
            yield conn

    return PostgresOperationJournal(connection)


def test_real_postgres_schema_create_lease_and_stage_cas(journal):
    operation = _operation()
    created = journal.create(operation)
    assert created.code is CreateCode.CREATED
    assert created.record == operation

    claimed = journal.claim_lease(operation.operation_id, 1, "worker-a", 10)
    assert claimed.code is LeaseCode.CLAIMED
    desired = claimed.record.transition(
        stage=OperationStage.INTENT_DURABLE,
        now=claimed.record.updated_at + 0.000001,
    )
    applied = journal.compare_and_swap(claimed.record, desired)
    assert applied.code is CasCode.APPLIED
    assert applied.record == desired
    assert journal.compare_and_swap(
        claimed.record, desired).code is CasCode.STALE_VERSION


def test_real_postgres_concurrent_claim_has_one_winner(journal):
    operation = _operation()
    assert journal.create(operation).code is CreateCode.CREATED

    def claim(owner):
        return journal.claim_lease(operation.operation_id, 1, owner, 10).code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("worker-a", "worker-b")))
    assert outcomes.count(LeaseCode.CLAIMED) == 1
    assert outcomes.count(LeaseCode.STALE_VERSION) == 1
