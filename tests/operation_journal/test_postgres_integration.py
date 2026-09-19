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
    RecoveryClaimCode,
)
from position_identity.slot import ExchangePositionKey

DSN = os.environ.get("OPERATION_JOURNAL_TEST_DSN")
ISOLATED = os.environ.get("OPERATION_JOURNAL_TEST_CONFIRM_ISOLATED") == "YES"
pytestmark = pytest.mark.skipif(
    not DSN or not ISOLATED,
    reason="requires explicit DSN and isolated-database confirmation",
)


def _operation(*, symbol="BTCUSDT", now=10.123456789):
    return OperationRecord.new(
        operation_id=str(uuid4()), operation_type=OperationType.OPEN,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="pg-integration", environment="SANDBOX",
            symbol=symbol,
        ),
        normalized_input={"quantity": "0.01"}, now=now,
        request_id=str(uuid4()),
    )


@pytest.fixture
def journal():
    import psycopg
    from psycopg import sql

    schema_name = f"operation_journal_test_{uuid4().hex}"
    schema = (
        Path(__file__).resolve().parents[2] /
        "db/postgres_operation_journal_schema.sql"
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

    yield PostgresOperationJournal(connection)
    with psycopg.connect(DSN) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
            sql.Identifier(schema_name)))


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


def test_real_postgres_recovery_claims_one_per_slot(journal):
    oldest = _operation(symbol="BTCUSDT", now=10)
    same_slot = _operation(symbol="BTCUSDT", now=20)
    other_slot = _operation(symbol="ETHUSDT", now=15)
    for operation in (oldest, same_slot, other_slot):
        assert journal.create(operation).code is CreateCode.CREATED

    result = journal.claim_recovery_batch("recovery", 10, 10)
    assert result.code is RecoveryClaimCode.CLAIMED
    assert {record.operation_id for record in result.records} == {
        oldest.operation_id, other_slot.operation_id,
    }
    assert journal.claim_lease(
        same_slot.operation_id, 1, "direct", 10).code is LeaseCode.BUSY


def test_real_postgres_two_recovery_workers_claim_disjoint_rows(journal):
    operations = [
        _operation(symbol=symbol, now=10 + index)
        for index, symbol in enumerate(
            ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"))
    ]
    for operation in operations:
        assert journal.create(operation).code is CreateCode.CREATED

    def recover(owner):
        return journal.claim_recovery_batch(owner, 10, 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recover, ("worker-a", "worker-b")))
    assert all(result.code is RecoveryClaimCode.CLAIMED for result in results)
    claimed = [
        record.operation_id for result in results for record in result.records]
    assert len(claimed) == 4
    assert set(claimed) == {operation.operation_id for operation in operations}
