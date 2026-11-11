"""Pure storage-monitor QA: no DB, credentials, network or trading callbacks."""

import json
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege, LockNotAvailable, QueryCanceled

from services.v2_queue_storage_health import (
    _FIELDS,
    INDEX_PRESSURE,
    MISMATCH,
    POLICY,
    PROJECTION,
    UNAVAILABLE,
    VACUUM_PRESSURE,
    assess,
)
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import scheduler_signal
from v2_core.evidence import canonical

NOW = 20_000_000
database = database_fixture


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self):
        self.autocommit = False
        self.transaction_count = 0
        self.calls = []
        self.failure = None
        self.count = 20
        self.pending_count = 20
        self.missing = 0
        self.extra = 0
        self.control = [(True, True, NOW - 1000)]
        self.triggers = [
            ("A", False, True, "v2_strategy_ready_sync_row", 0, 29, True, 0, "")
        ]
        self.relations = {}
        for oid, name in enumerate(
            ("v2_strategy_tasks", "v2_strategy_ready", "v2_strategy_ready_control"), 1
        ):
            self.relations[name] = dict(
                zip(
                    _FIELDS,
                    (
                        name,
                        oid,
                        "isolated_qa",
                        "r",
                        100000,
                        100,
                        NOW - 120000,
                        NOW - 240000,
                        3,
                        4,
                        65536,
                        1000,
                    ),
                    strict=True,
                )
            )

    @contextmanager
    def connect(self):
        yield self

    @contextmanager
    def transaction(self):
        self.transaction_count += 1
        yield self

    def execute(self, query, parameters=None):
        def render(value):
            if isinstance(value, str):
                return value
            if isinstance(value, sql.SQL):
                return value._obj
            if isinstance(value, sql.Identifier):
                return ".".join(
                    '"' + part.replace('"', '""') + '"' for part in value._obj
                )
            return "".join(render(part) for part in value._obj)

        text = render(query)
        self.calls.append((text, parameters))
        if text.startswith("SET "):
            return Result([])
        if self.failure:
            raise self.failure
        if "WITH wanted" in text:
            return Result(
                [
                    tuple(row[field] for field in _FIELDS)
                    for row in self.relations.values()
                ]
            )
        if "SELECT singleton" in text:
            return Result(self.control)
        if "pg_catalog.pg_trigger" in text:
            return Result(self.triggers)
        if "WITH pending AS MATERIALIZED" in text:
            return Result([(self.count, self.pending_count, self.missing, self.extra)])
        raise AssertionError("unexpected SQL")


def sample(connection, previous=None):
    return assess(connection.connect, now_ms=NOW, previous=previous)


def test_readonly_bounded_sample_does_not_claim_integrity_or_latency():
    connection = Connection()
    result = sample(connection)
    assert result["status"] == "AVAILABLE"
    assert result["active"] == []
    assert result["projection_integrity"]["status"] == "PROVEN_AT_SNAPSHOT"
    assert result["claim_p95"]["status"] == "NOT_PROVIDED"
    assert "ESTIMATES" in result["statistics_basis"]
    assert connection.calls[:4] == [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL statement_timeout='200ms'", None),
        ("SET LOCAL lock_timeout='50ms'", None),
        ("SET LOCAL max_parallel_workers_per_gather=0", None),
    ]
    assert len(connection.calls) == 8
    count_sql, parameters = connection.calls[-1]
    assert 'FROM "isolated_qa"."v2_strategy_ready"' in count_sql
    assert "ORDER BY consumer,signal_id LIMIT %s" in count_sql
    assert "WHERE completed_at IS NULL" in count_sql
    assert "ORDER BY consumer,next_attempt_at,signal_id LIMIT %s" in count_sql
    assert parameters == (10001, 10001)
    catalog_sql = connection.calls[4][0]
    assert "n.nspname=pg_catalog.current_schema()" in catalog_sql
    assert "c.relnamespace=n.oid AND c.relname=w.name" in catalog_sql
    assert "to_regclass" not in catalog_sql
    for query, _ in connection.calls:
        assert "FROM v2_strategy_tasks" not in query
        assert "FOR UPDATE" not in query
        assert not query.lstrip().startswith(("INSERT", "UPDATE", "DELETE", "VACUUM"))
    assert json.loads(canonical(result)) == result


def test_autocommit_connection_gets_real_readonly_transaction():
    connection = Connection()
    connection.autocommit = True
    assert sample(connection)["status"] == "AVAILABLE"
    assert connection.transaction_count == 1


@pytest.mark.parametrize(
    "count,kind,status",
    [(10000, "EXACT", "AVAILABLE"), (10001, "LOWER_BOUND", "UNKNOWN")],
)
def test_count_cap_is_lower_bound_not_a_ratio_proof(count, kind, status):
    connection = Connection()
    connection.count = count
    result = sample(connection)
    assert result["ready"]["count_kind"] == kind
    assert result["status"] == status
    assert (UNAVAILABLE in result["active"]) is (status == "UNKNOWN")


def test_capped_count_does_not_falsely_prove_large_bytes_per_ready_or_recover():
    connection = Connection()
    connection.count = 10001
    connection.relations["v2_strategy_ready"]["index_bytes"] = 1024**3
    result = sample(connection)
    assert result["ready"]["index_bytes_per_ready"] is None
    assert INDEX_PRESSURE not in result["active"]
    assert INDEX_PRESSURE in sample(connection, {"active": [INDEX_PRESSURE]})["active"]


@pytest.mark.parametrize(
    "count,index_bytes,warning",
    [
        (0, 65536, False),
        (0, 16 * 1024**2, True),
        (1, 16 * 1024**2 - 1, False),
        (1024, 16 * 1024**2, False),
        (1023, 16 * 1024**2, True),
    ],
)
def test_absolute_and_per_row_thresholds_and_empty_queue(count, index_bytes, warning):
    connection = Connection()
    connection.count = count
    connection.relations["v2_strategy_ready"]["index_bytes"] = index_bytes
    result = sample(connection)
    assert (INDEX_PRESSURE in result["active"]) is warning
    if count == 0:
        assert result["ready"]["index_bytes_per_ready"] is None
        assert result["ready"]["ratio_status"] == "UNDEFINED_EMPTY_READY"


@pytest.mark.parametrize(
    "dead,age,warning",
    [
        (100000, 3600000, False),
        (100001, 3599999, False),
        (100001, 3600000, True),
    ],
)
def test_vacuum_warning_requires_both_dead_pressure_and_old_vacuum(dead, age, warning):
    connection = Connection()
    row = connection.relations["v2_strategy_tasks"]
    row.update(
        estimated_dead_tuples=dead, last_vacuum_ms=None, last_autovacuum_ms=NOW - age
    )
    result = sample(connection)
    assert (VACUUM_PRESSURE in result["active"]) is warning


def test_recent_manual_vacuum_is_not_reported_as_stale_autovacuum():
    connection = Connection()
    row = connection.relations["v2_strategy_tasks"]
    row.update(
        estimated_dead_tuples=200000,
        last_vacuum_ms=NOW - 1000,
        last_autovacuum_ms=NOW - POLICY.vacuum_stale_ms * 2,
    )
    assert VACUUM_PRESSURE not in sample(connection)["active"]


def test_missing_vacuum_history_with_high_dead_estimate_is_unknown():
    connection = Connection()
    row = connection.relations["v2_strategy_tasks"]
    row.update(
        estimated_dead_tuples=200000, last_vacuum_ms=None, last_autovacuum_ms=None
    )
    result = sample(connection, {"active": [VACUUM_PRESSURE]})
    assert result["status"] == "UNKNOWN"
    assert (
        result["vacuum"]["v2_strategy_tasks"]["reason"] == "VACUUM_HISTORY_UNAVAILABLE"
    )
    assert set(result["active"]) == {UNAVAILABLE, VACUUM_PRESSURE}


@pytest.mark.parametrize(
    "change",
    [
        {"stats_reset_at_ms": NOW - 1},
        {"oid": 99},
        {"vacuum_count": 0},
        {"autovacuum_count": 0},
    ],
)
def test_stats_reset_or_relation_epoch_change_cannot_clear_old_pressure(change):
    connection = Connection()
    previous = sample(connection)
    previous["active"] = [VACUUM_PRESSURE]
    connection.relations["v2_strategy_tasks"].update(change)
    result = sample(connection, previous)
    assert result["status"] == "UNKNOWN"
    assert VACUUM_PRESSURE in result["active"]


@pytest.mark.parametrize(
    "triggers",
    [
        [],
        [("O", False, True)],
        [("D", False, True)],
        [("A", True, True)],
        [("A", False, False)],
    ],
)
def test_projection_trigger_must_be_always_on_expected_table_and_schema(triggers):
    connection = Connection()
    connection.triggers = triggers
    result = sample(connection)
    assert result["status"] == "DEGRADED"
    assert result["active"] == [PROJECTION]
    query, parameters = connection.calls[-2]
    assert "t.tgrelid=%s" in query
    assert parameters == (connection.relations["v2_strategy_tasks"]["oid"],)


@pytest.mark.parametrize(
    "control,status",
    [
        ([], "UNKNOWN"),
        ([(False, True, 1)], "UNKNOWN"),
        ([(True, False, 1)], "DEGRADED"),
        ([(True, True, None)], "UNKNOWN"),
        ([(True, True, NOW + 1)], "UNKNOWN"),
        ([(True, True, 1), (False, True, 1)], "UNKNOWN"),
    ],
)
def test_missing_disabled_unvalidated_or_future_control_is_not_clear(control, status):
    connection = Connection()
    connection.control = control
    result = sample(connection)
    assert result["status"] == status
    assert PROJECTION in result["active"]


@pytest.mark.parametrize(
    "field,value", [("oid", None), ("kind", "v"), ("schema", "wrong_schema")]
)
def test_missing_relation_or_mixed_schema_is_unknown_without_business_scan(
    field, value
):
    connection = Connection()
    connection.relations["v2_strategy_ready"][field] = value
    result = sample(connection)
    assert result["status"] == "UNKNOWN"
    assert set(result["active"]) == {UNAVAILABLE, PROJECTION}
    assert len(connection.calls) == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_dead_tuples", None),
        ("estimated_live_tuples", -1),
        ("last_autovacuum_ms", NOW + 1),
        ("vacuum_count", True),
    ],
)
def test_unavailable_or_invalid_stats_are_not_coerced_to_zero(field, value):
    connection = Connection()
    connection.relations["v2_strategy_tasks"][field] = value
    assert sample(connection)["status"] == "UNKNOWN"


@pytest.mark.parametrize(
    "error,reason",
    [
        (QueryCanceled("private SQL"), "QUERY_TIMEOUT"),
        (LockNotAvailable("private SQL"), "LOCK_TIMEOUT"),
        (InsufficientPrivilege("private SQL"), "READ_FAILED"),
    ],
)
def test_read_failure_retains_old_alarms_without_leaking_raw_error(error, reason):
    connection = Connection()
    connection.failure = error
    previous = {"active": [INDEX_PRESSURE, VACUUM_PRESSURE, "UNRELATED_ALARM"]}
    result = sample(connection, previous)
    assert result["status"] == "UNKNOWN"
    assert result["reason"] == reason
    assert set(result["active"]) == {UNAVAILABLE, INDEX_PRESSURE, VACUUM_PRESSURE}
    assert "private" not in json.dumps(result)


def test_pressure_unknown_then_fresh_healthy_recovery_is_explicit():
    connection = Connection()
    connection.relations["v2_strategy_ready"]["index_bytes"] = 16 * 1024**2
    alarm = sample(connection)
    assert INDEX_PRESSURE in alarm["active"]
    connection.failure = QueryCanceled()
    unknown = sample(connection, alarm)
    assert INDEX_PRESSURE in unknown["active"]
    connection.failure = None
    connection.relations["v2_strategy_ready"]["index_bytes"] = 65536
    healthy = sample(connection, unknown)
    assert healthy["status"] == "AVAILABLE"
    assert healthy["active"] == []


@pytest.mark.parametrize("now", [True, -1, None, "1"])
def test_invalid_clock_does_not_open_connection(now):
    def forbidden():
        raise AssertionError("must not connect")

    with pytest.raises(ValueError, match="QUEUE_STORAGE_CLOCK_INVALID"):
        assess(forbidden, now_ms=now)


def test_caller_previous_snapshot_not_mutated():
    connection = Connection()
    previous = sample(connection)
    saved = deepcopy(previous)
    sample(connection, previous)
    assert previous == saved


@pytest.mark.parametrize("missing,extra", [(1, 0), (0, 1), (1, 1)])
def test_integrity_is_two_way_not_equal_count_only(missing, extra):
    connection = Connection()
    connection.missing, connection.extra = missing, extra
    result = sample(connection)
    assert result["projection_integrity"]["status"] == "MISMATCH"
    assert result["status"] == "DEGRADED"
    assert MISMATCH in result["active"]
    query = connection.calls[-1][0]
    assert "WHERE NOT EXISTS" in query
    assert "LEFT JOIN LATERAL" in query
    assert "t.signal_id IS NULL OR t.completed_at IS NOT NULL" in query
    assert query.count("OFFSET 0") == 2


@pytest.mark.parametrize("field", ["count", "pending_count"])
def test_either_integrity_set_capped_is_unknown_and_keeps_old_mismatch(field):
    connection = Connection()
    setattr(connection, field, 10001)
    result = sample(connection, {"active": [MISMATCH]})
    assert result["status"] == "UNKNOWN"
    assert result["projection_integrity"]["status"] == "UNKNOWN"
    assert set(result["active"]) == {MISMATCH, UNAVAILABLE}


@pytest.mark.parametrize(
    "change",
    [
        {"oid": 99},
        {"vacuum_count": 0, "autovacuum_count": 0},
    ],
)
def test_epoch_warmup_survives_repeated_samples_and_query_failure(change):
    connection = Connection()
    previous = sample(connection)
    previous["active"] = [VACUUM_PRESSURE]
    connection.relations["v2_strategy_tasks"].update(change)
    first = sample(connection, previous)
    assert first["status"] == "UNKNOWN"
    second = assess(connection.connect, now_ms=NOW + 1, previous=first)
    assert second["status"] == "UNKNOWN"
    assert VACUUM_PRESSURE in second["active"]
    connection.failure = QueryCanceled()
    unavailable = assess(connection.connect, now_ms=NOW + 2, previous=second)
    assert unavailable["relations_status"] == "STALE"
    connection.failure = None
    final = assess(connection.connect, now_ms=NOW + 60000, previous=unavailable)
    assert final["status"] == "AVAILABLE"
    assert final["active"] == []


@pytest.mark.parametrize("broken", ["missing_stats", "missing_relation"])
def test_epoch_warmup_survives_unavailable_statistics_or_relation(broken):
    connection = Connection()
    previous = sample(connection)
    previous["active"] = [VACUUM_PRESSURE]
    connection.relations["v2_strategy_tasks"]["vacuum_count"] = 0
    reset = sample(connection, previous)
    field = "estimated_dead_tuples" if broken == "missing_stats" else "oid"
    original = connection.relations["v2_strategy_tasks"][field]
    connection.relations["v2_strategy_tasks"][field] = None
    unknown = assess(connection.connect, now_ms=NOW + 1, previous=reset)
    connection.relations["v2_strategy_tasks"][field] = original
    still_warming = assess(connection.connect, now_ms=NOW + 2, previous=unknown)
    assert still_warming["status"] == "UNKNOWN"
    assert VACUUM_PRESSURE in still_warming["active"]
    assert (
        still_warming["relations"]["v2_strategy_tasks"]["epoch_warmup_started_at_ms"]
        == NOW
    )


@pytest.mark.parametrize(
    "field,value",
    [(3, "wrong_same_schema_function"), (4, 1), (5, 5), (6, False), (7, 1), (8, "3")],
)
def test_same_schema_wrong_trigger_function_or_events_are_not_accepted(field, value):
    connection = Connection()
    trigger = list(connection.triggers[0])
    trigger[field] = value
    connection.triggers = [tuple(trigger)]
    result = sample(connection)
    assert result["active"] == [PROJECTION]


def test_dead_thresholds_do_not_scale_with_retained_history():
    connection = Connection()
    for name, threshold in (("v2_strategy_tasks", 100000), ("v2_strategy_ready", 1000)):
        connection.relations[name].update(
            estimated_live_tuples=10**12,
            estimated_dead_tuples=threshold + 1,
            last_vacuum_ms=None,
            last_autovacuum_ms=NOW - POLICY.vacuum_stale_ms,
        )
    result = sample(connection)
    assert result["active"] == [VACUUM_PRESSURE]
    assert result["vacuum"]["v2_strategy_tasks"]["estimated_dead_threshold"] == 100000
    assert result["vacuum"]["v2_strategy_ready"]["estimated_dead_threshold"] == 1000


@pytest.fixture
def projection_database(database):
    # The shared fixture enforces an isolated Unix socket and cluster identity.
    # These setup writes can never target a deployed or production database.
    migration = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20261110_strategy_ready_queue.sql"
    )
    with database() as conn:
        conn.execute(migration.read_text())
    pending = scheduler_signal(database, "storage-pending")
    completed = scheduler_signal(database, "storage-completed")
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id) VALUES('storage-qa',%s)",
            (pending,),
        )
        conn.execute(
            "INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at) VALUES('storage-qa',%s,clock_timestamp())",
            (completed,),
        )
        assert conn.execute("SELECT v2_strategy_ready_enable()").fetchone() == (True,)
    return database, pending, completed


def test_isolated_postgres_catalog_and_snapshot_query(projection_database):
    connect, _, _ = projection_database
    result = assess(connect, now_ms=time.time_ns() // 1000000)
    assert result["projection"]["status"] == "AVAILABLE"
    assert result["projection_integrity"]["status"] == "PROVEN_AT_SNAPSHOT"
    assert result["projection_integrity"]["pending_count"] == 1
    assert result["projection_integrity"]["ready_count"] == 1


def test_isolated_postgres_equal_counts_but_different_membership_is_mismatch(
    projection_database,
):
    connect, pending, completed = projection_database
    with connect() as conn:
        conn.execute(
            "DELETE FROM v2_strategy_ready WHERE consumer='storage-qa' AND signal_id=%s",
            (pending,),
        )
        conn.execute(
            "INSERT INTO v2_strategy_ready(consumer,signal_id) VALUES('storage-qa',%s)",
            (completed,),
        )
    result = assess(connect, now_ms=time.time_ns() // 1000000)
    integrity = result["projection_integrity"]
    assert integrity["pending_count"] == integrity["ready_count"] == 1
    assert integrity["missing_ready"] == integrity["unexpected_ready"] == 1
    assert integrity["status"] == "MISMATCH"
    assert MISMATCH in result["active"]
