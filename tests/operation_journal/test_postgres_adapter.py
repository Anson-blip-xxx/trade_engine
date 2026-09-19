from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

from operation_journal import (
    CasCode,
    CreateCode,
    OperationRecord,
    OperationStage,
    OperationType,
    PostgresOperationJournal,
    ReadCode,
)
from position_identity.slot import ExchangePositionKey


def _operation(operation_id=None):
    return OperationRecord.new(
        operation_id=operation_id or str(uuid4()),
        operation_type=OperationType.OPEN,
        exchange_position_key=ExchangePositionKey.one_way(
            account_principal_id="journal-adapter", environment="SANDBOX",
            symbol="ETHUSDT",
        ),
        normalized_input={"quantity": 1}, now=10, request_id="request-7",
    )


def _row(params):
    return {
        "operation_id": params["operation_id"],
        "schema_version": params["schema_version"],
        "operation_type": params["operation_type"],
        "slot_digest": params["slot_digest"],
        "exchange_position_key": params["exchange_position_key"],
        "request_id": params["request_id"],
        "position_episode_id": params["position_episode_id"],
        "lifecycle_generation": params["lifecycle_generation"],
        "protection_generation": params["protection_generation"],
        "stage": params["stage"],
        "version": params["version"],
        "owner_token": params["owner_token"],
        "lease_expires_at": params["lease_expires_at"],
        "input": params["input"],
        "exchange_aliases": params["exchange_aliases"],
        "effect_summary": params["effect_summary"],
        "pending_requirements": params["pending_requirements"],
        "last_error": params["last_error"],
        "next_attempt_at": params["next_attempt_at"],
        "created_at": params["created_at"],
        "updated_at": params["updated_at"],
    }


class _Cursor:
    def __init__(self, database):
        self.database = database
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        if self.database.fail_execute:
            raise OSError("database unavailable")
        normalized = " ".join(sql.split()).upper()
        operation_id = params["operation_id"]
        if normalized.startswith("INSERT"):
            if operation_id in self.database.rows:
                self.result = None
            else:
                self.result = _row(params)
                self.database.rows[operation_id] = self.result
        elif normalized.startswith("UPDATE"):
            current = self.database.rows.get(operation_id)
            matches = current is not None and (
                current["version"] == params["expected_version"] and
                current["owner_token"] == params["expected_owner_token"]
            )
            if matches:
                self.result = _row(params)
                self.database.rows[operation_id] = self.result
            else:
                self.result = None
        elif normalized.startswith("SELECT"):
            self.result = self.database.rows.get(operation_id)
        else:
            raise AssertionError(normalized)

    def fetchone(self):
        if self.result is not None and self.database.tuple_rows:
            return tuple(self.result.values())
        return self.result


class _Connection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return _Cursor(self.database)


class _Database:
    def __init__(self):
        self.rows = {}
        self.fail_execute = False
        self.fail_commit = False
        self.tuple_rows = False

    @contextmanager
    def connection(self):
        yield _Connection(self)
        if self.fail_commit:
            raise OSError("commit acknowledgement lost")


def _store(database=None):
    database = database or _Database()
    return PostgresOperationJournal(database.connection), database


def test_create_is_idempotent_but_rejects_identity_collision():
    store, _ = _store()
    operation = _operation()
    created = store.create(operation)
    assert created.code is CreateCode.CREATED
    assert created.record == operation
    duplicate = store.create(operation)
    assert duplicate.code is CreateCode.ALREADY_EXISTS
    assert duplicate.record == operation
    conflict = store.create(replace(operation, input_json='{"quantity":2}'))
    assert conflict.code is CreateCode.CONFLICT
    assert conflict.record == operation


def test_read_reports_found_not_found_and_unavailable_separately():
    store, database = _store()
    operation = _operation()
    assert store.read(operation.operation_id).code is ReadCode.NOT_FOUND
    store.create(operation)
    found = store.read(operation.operation_id)
    assert found.code is ReadCode.FOUND and found.record == operation
    database.fail_execute = True
    assert store.read(operation.operation_id).code is ReadCode.UNAVAILABLE


def test_default_driver_tuple_rows_round_trip():
    store, database = _store()
    operation = _operation()
    assert store.create(operation).code is CreateCode.CREATED
    database.tuple_rows = True
    found = store.read(operation.operation_id)
    assert found.code is ReadCode.FOUND
    assert found.record == operation


def test_cas_applies_one_transition_with_version_and_owner_fencing():
    store, _ = _store()
    new = _operation()
    store.create(new)
    operation = new.transition(
        stage=OperationStage.INTENT_DURABLE, now=11,
        owner_token="owner-a", lease_expires_at=30,
    )
    assert store.compare_and_swap(new, operation).code is CasCode.APPLIED
    desired = operation.transition(stage=OperationStage.SUBMITTING, now=12)
    applied = store.compare_and_swap(operation, desired)
    assert applied.code is CasCode.APPLIED
    assert applied.record == desired

    stale = store.compare_and_swap(operation, desired)
    assert stale.code is CasCode.STALE_VERSION
    assert stale.record == desired


def test_cas_distinguishes_owner_mismatch_and_not_found():
    store, _ = _store()
    new = _operation()
    store.create(new)
    operation = new.transition(
        stage=OperationStage.INTENT_DURABLE, now=11,
        owner_token="owner-a", lease_expires_at=30,
    )
    assert store.compare_and_swap(new, operation).code is CasCode.APPLIED
    wrong_owner = replace(operation, owner_token="owner-b")
    desired = wrong_owner.transition(stage=OperationStage.SUBMITTING, now=12)
    result = store.compare_and_swap(wrong_owner, desired)
    assert result.code is CasCode.OWNER_MISMATCH
    assert result.record == operation

    missing = _operation().transition(stage=OperationStage.INTENT_DURABLE, now=11)
    wanted = missing.transition(stage=OperationStage.SUBMITTING, now=12)
    assert store.compare_and_swap(missing, wanted).code is CasCode.NOT_FOUND


def test_write_exception_is_unknown_even_if_effect_may_have_committed():
    store, database = _store()
    database.fail_commit = True
    operation = _operation()
    result = store.create(operation)
    assert result.code is CreateCode.UNKNOWN
    assert operation.operation_id in database.rows

    database.fail_commit = False
    current = store.read(operation.operation_id).record
    desired = current.transition(stage=OperationStage.INTENT_DURABLE, now=11)
    database.fail_commit = True
    cas = store.compare_and_swap(current, desired)
    assert cas.code is CasCode.UNKNOWN
    assert database.rows[operation.operation_id]["stage"] == "INTENT_DURABLE"


def test_cas_rejects_invalid_transition_pair_before_database_io():
    store, database = _store()
    expected = _operation()
    with pytest.raises(ValueError, match="advance exactly once"):
        store.compare_and_swap(expected, expected)
    other = replace(expected, operation_id=str(uuid4()), version=2)
    with pytest.raises(ValueError, match="operation_id mismatch"):
        store.compare_and_swap(expected, other)
    illegal = replace(expected, stage=OperationStage.COMPLETED, version=2)
    with pytest.raises(ValueError, match="stage transition is illegal"):
        store.compare_and_swap(expected, illegal)
    assert database.rows == {}


def test_create_rejects_pre_advanced_record():
    store, database = _store()
    advanced = _operation().transition(stage=OperationStage.INTENT_DURABLE, now=11)
    with pytest.raises(ValueError, match="NEW version-1"):
        store.create(advanced)
    assert database.rows == {}
