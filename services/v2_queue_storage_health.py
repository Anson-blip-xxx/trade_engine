"""Bounded, read-only queue storage telemetry; never repairs or grants trading.

The two pending sets are compared only within explicit caps. PostgreSQL tuple
statistics are estimates, not a bloat measurement. Canonical reads use pending
index enumeration and primary-key probes, never a whole-history count.
"""

from contextlib import nullcontext
from dataclasses import asdict, dataclass

from psycopg import sql
from psycopg.errors import LockNotAvailable, QueryCanceled


@dataclass(frozen=True)
class StoragePolicy:
    ready_count_limit: int = 10000
    minimum_index_bytes: int = 16 * 1024 * 1024
    index_bytes_per_ready: int = 16 * 1024
    tasks_dead_threshold: int = 100000
    ready_dead_threshold: int = 1000
    vacuum_stale_ms: int = 3600000
    stats_warmup_ms: int = 60000


POLICY = StoragePolicy()
UNAVAILABLE = "QUEUE_STORAGE_UNAVAILABLE"
PROJECTION = "QUEUE_READY_PROJECTION_UNAVAILABLE"
INDEX_PRESSURE = "QUEUE_READY_INDEX_PRESSURE"
VACUUM_PRESSURE = "QUEUE_VACUUM_PRESSURE"
MISMATCH = "QUEUE_PROJECTION_MISMATCH"
_CODES = frozenset({UNAVAILABLE, PROJECTION, INDEX_PRESSURE, VACUUM_PRESSURE, MISMATCH})
_RELATIONS = (
    "v2_strategy_tasks",
    "v2_strategy_ready",
    "v2_strategy_ready_control",
)
_FIELDS = (
    "name",
    "oid",
    "schema",
    "kind",
    "estimated_live_tuples",
    "estimated_dead_tuples",
    "last_vacuum_ms",
    "last_autovacuum_ms",
    "vacuum_count",
    "autovacuum_count",
    "index_bytes",
    "stats_reset_at_ms",
)

_CATALOG_SQL = """WITH wanted(name) AS (
    VALUES ('v2_strategy_tasks'),('v2_strategy_ready'),('v2_strategy_ready_control'))
SELECT w.name,c.oid,n.nspname,c.relkind,s.n_live_tup,s.n_dead_tup,
    floor(extract(epoch FROM s.last_vacuum)*1000)::bigint,
    floor(extract(epoch FROM s.last_autovacuum)*1000)::bigint,
    s.vacuum_count,s.autovacuum_count,
    CASE WHEN c.oid IS NOT NULL THEN pg_catalog.pg_indexes_size(c.oid) END,
    floor(extract(epoch FROM d.stats_reset)*1000)::bigint
FROM wanted w
LEFT JOIN pg_catalog.pg_namespace n ON n.nspname=pg_catalog.current_schema()
LEFT JOIN pg_catalog.pg_class c ON c.relnamespace=n.oid AND c.relname=w.name
LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid=c.oid
LEFT JOIN pg_catalog.pg_stat_database d ON d.datname=pg_catalog.current_database()
ORDER BY w.name"""


def _old_codes(previous):
    active = previous.get("active", []) if isinstance(previous, dict) else []
    return {code for code in active if isinstance(code, str) and code in _CODES}


def _base(now):
    return {
        "status": "UNKNOWN",
        "observed_at_ms": now,
        "active": [],
        "policy": asdict(POLICY),
        "statement_timeout_ms": 200,
        "lock_timeout_ms": 50,
        "timeout_scope": "PER_STATEMENT_NOT_CONNECT_OR_TOTAL_CALL",
        "projection_integrity": {"status": "UNKNOWN", "reason": "NOT_SAMPLED"},
        "claim_p95": {"status": "NOT_PROVIDED", "reason": "NO_PER_CLAIM_SAMPLES"},
        "statistics_basis": "POSTGRES_ESTIMATES_NOT_EXACT_BLOAT",
    }


def _read(connect):
    with connect() as conn:  # noqa: SIM117 - transaction policy needs the entered connection
        # Explicit BEGIN is needed for an autocommit connection. Ordinary
        # connectors may already have SET search_path in their transaction.
        with conn.transaction() if conn.autocommit else nullcontext():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='200ms'")
            conn.execute("SET LOCAL lock_timeout='50ms'")
            conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
            rows = conn.execute(_CATALOG_SQL).fetchall()
            relations = {row[0]: dict(zip(_FIELDS, row, strict=True)) for row in rows}
            if set(relations) != set(_RELATIONS) or any(
                not row["oid"] or row["kind"] != "r" for row in relations.values()
            ):
                return relations, None, None, None, "RELATION_MISSING_OR_INVALID"
            schemas = {row["schema"] for row in relations.values()}
            if len(schemas) != 1:
                return relations, None, None, None, "RELATION_SCHEMA_MISMATCH"
            schema = schemas.pop()
            control = conn.execute(
                sql.SQL(
                    "SELECT singleton,enabled,"
                    "floor(extract(epoch FROM validated_at)*1000)::bigint "
                    "FROM {} LIMIT 2"
                ).format(sql.Identifier(schema, "v2_strategy_ready_control"))
            ).fetchall()
            triggers = conn.execute(
                """SELECT t.tgenabled,t.tgisinternal,p.pronamespace=c.relnamespace,
                    p.proname,p.pronargs,t.tgtype,t.tgqual IS NULL,t.tgnargs,t.tgattr::text
                FROM pg_catalog.pg_trigger t
                JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
                JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid
                WHERE t.tgrelid=%s AND t.tgname='v2_strategy_ready_sync'""",
                (relations["v2_strategy_tasks"]["oid"],),
            ).fetchall()
            counted = conn.execute(
                sql.SQL(
                    """WITH pending AS MATERIALIZED (
                        SELECT consumer,signal_id FROM {tasks}
                        WHERE completed_at IS NULL
                        ORDER BY consumer,next_attempt_at,signal_id LIMIT %s),
                    ready AS MATERIALIZED (
                        SELECT consumer,signal_id FROM {ready}
                        ORDER BY consumer,signal_id LIMIT %s)
                    SELECT (SELECT count(*) FROM ready),
                        (SELECT count(*) FROM pending),
                        (SELECT count(*) FROM pending p WHERE NOT EXISTS (
                            SELECT 1 FROM {ready} q
                            WHERE (q.consumer,q.signal_id)=(p.consumer,p.signal_id)
                            OFFSET 0)),
                        (SELECT count(*) FROM ready q LEFT JOIN LATERAL (
                            SELECT t.signal_id,t.completed_at FROM {tasks} t
                            WHERE (t.consumer,t.signal_id)=(q.consumer,q.signal_id)
                            OFFSET 0) t ON true
                            WHERE t.signal_id IS NULL OR t.completed_at IS NOT NULL)"""
                ).format(
                    tasks=sql.Identifier(schema, "v2_strategy_tasks"),
                    ready=sql.Identifier(schema, "v2_strategy_ready"),
                ),
                (POLICY.ready_count_limit + 1,) * 2,
            ).fetchone()
            return relations, control, triggers, counted, None


def _projection(control, triggers, now):
    if len(control) != 1 or control[0][0] is not True:
        return {"status": "UNKNOWN", "reason": "CONTROL_MISSING_OR_INVALID"}
    _, enabled, validated = control[0]
    if type(enabled) is not bool:
        return {"status": "UNKNOWN", "reason": "CONTROL_MISSING_OR_INVALID"}
    if not enabled:
        return {"status": "DEGRADED", "reason": "PROJECTION_DISABLED"}
    if type(validated) is not int or not 0 <= validated <= now:
        return {"status": "UNKNOWN", "reason": "VALIDATION_TIMESTAMP_UNAVAILABLE"}
    # 29 = AFTER ROW INSERT|DELETE|UPDATE; a WHEN restriction is not accepted.
    if triggers != [
        ("A", False, True, "v2_strategy_ready_sync_row", 0, 29, True, 0, "")
    ]:
        return {
            "status": "DEGRADED",
            "reason": "ALWAYS_SYNC_TRIGGER_MISSING_OR_INVALID",
        }
    return {
        "status": "AVAILABLE",
        "reason": "STRUCTURAL_PREREQUISITES_ONLY",
        "validated_at_ms": validated,
    }


def _vacuum(row, previous, now):
    warmup_started = (
        previous.get("epoch_warmup_started_at_ms")
        if isinstance(previous, dict)
        else None
    )
    row["epoch_warmup_started_at_ms"] = warmup_started

    def unavailable(reason):
        # A missing statistic must not erase an in-progress reset warmup. A
        # fresh invalid sample also invalidates the baseline for one window.
        row["epoch_warmup_started_at_ms"] = (
            warmup_started
            if type(warmup_started) is int
            and 0 <= now - warmup_started < POLICY.stats_warmup_ms
            else now
        )
        return {"status": "UNKNOWN", "reason": reason}

    names = (
        "estimated_live_tuples",
        "estimated_dead_tuples",
        "vacuum_count",
        "autovacuum_count",
    )
    if any(type(row[name]) is not int or row[name] < 0 for name in names):
        return unavailable("TABLE_STATISTICS_UNAVAILABLE")
    timestamps = (
        row["last_vacuum_ms"],
        row["last_autovacuum_ms"],
        row["stats_reset_at_ms"],
    )
    if any(
        value is not None and (type(value) is not int or not 0 <= value <= now)
        for value in timestamps
    ):
        return unavailable("TABLE_STATISTICS_TIMESTAMP_INVALID")
    reset = row["stats_reset_at_ms"]
    reset_recently = reset is not None and now - reset < POLICY.stats_warmup_ms
    epoch_changed = (
        isinstance(previous, dict)
        and bool(previous)
        and (
            previous.get("oid") != row["oid"]
            or previous.get("stats_reset_at_ms") != reset
            or any(
                type(previous.get(name)) is int and previous[name] > row[name]
                for name in ("vacuum_count", "autovacuum_count")
            )
        )
    )
    if epoch_changed:
        warmup_started = now
    if reset_recently:
        warmup_started = max(reset, warmup_started or 0)
    if warmup_started is not None and (
        type(warmup_started) is not int or not 0 <= warmup_started <= now
    ):
        return {"status": "UNKNOWN", "reason": "STATISTICS_EPOCH_TIMESTAMP_INVALID"}
    row["epoch_warmup_started_at_ms"] = warmup_started
    if warmup_started is not None and now - warmup_started < POLICY.stats_warmup_ms:
        return {"status": "UNKNOWN", "reason": "STATISTICS_EPOCH_WARMUP"}
    threshold = (
        POLICY.tasks_dead_threshold
        if row["name"] == "v2_strategy_tasks"
        else POLICY.ready_dead_threshold
    )
    known = [value for value in timestamps[:2] if value is not None]
    age = now - max(known) if known else None
    result = {
        "status": "AVAILABLE",
        "estimated_dead_threshold": threshold,
        "last_vacuum_age_ms": age,
    }
    if row["estimated_dead_tuples"] > threshold:
        if age is None:
            result.update(status="UNKNOWN", reason="VACUUM_HISTORY_UNAVAILABLE")
        elif age >= POLICY.vacuum_stale_ms:
            result.update(status="DEGRADED", reason="ESTIMATED_DEAD_AND_OLD_VACUUM")
    return result


def _index(counted, index_bytes):
    if (
        not counted
        or type(counted[0]) is not int
        or not 0 <= counted[0] <= POLICY.ready_count_limit + 1
        or type(index_bytes) is not int
        or index_bytes < 0
    ):
        return {"status": "UNKNOWN", "reason": "READY_MEASUREMENT_INVALID"}
    count = counted[0]
    exact = count <= POLICY.ready_count_limit
    result = {
        "status": "AVAILABLE",
        "count": count,
        "count_kind": "EXACT" if exact else "LOWER_BOUND",
        "index_bytes": index_bytes,
        "index_bytes_per_ready": None,
    }
    if not exact:
        # bytes/lower_bound is an UPPER bound, not proof of excess per row.
        result.update(status="UNKNOWN", reason="READY_COUNT_CAPPED_RATIO_UNKNOWN")
    elif count == 0:
        result["ratio_status"] = "UNDEFINED_EMPTY_READY"
        if index_bytes >= POLICY.minimum_index_bytes:
            result.update(status="DEGRADED", reason="LARGE_INDEX_EMPTY_READY")
    else:
        result["index_bytes_per_ready"] = index_bytes // count
        if (
            index_bytes >= POLICY.minimum_index_bytes
            and index_bytes > count * POLICY.index_bytes_per_ready
        ):
            result.update(status="DEGRADED", reason="LARGE_INDEX_PER_READY")
    return result


def _integrity(counted):
    if (
        not counted
        or len(counted) != 4
        or any(
            type(value) is not int or not 0 <= value <= POLICY.ready_count_limit + 1
            for value in counted
        )
        or counted[2] > counted[1]
        or counted[3] > counted[0]
    ):
        return {"status": "UNKNOWN", "reason": "INTEGRITY_MEASUREMENT_INVALID"}
    ready, pending, missing, extra = counted
    result = {
        "ready_count": ready,
        "pending_count": pending,
        "missing_ready": missing,
        "unexpected_ready": extra,
        "count_kind": "LOWER_BOUND"
        if max(ready, pending) > POLICY.ready_count_limit
        else "EXACT",
        "status": "MISMATCH" if missing or extra else "PROVEN_AT_SNAPSHOT",
        "basis": "SAME_STATEMENT_TWO_WAY_PRIMARY_KEY_CHECKS",
    }
    if result["count_kind"] == "LOWER_BOUND":
        result.update(status="UNKNOWN", reason="INTEGRITY_SAMPLE_CAPPED")
    return result


def assess(connect, *, now_ms, previous=None):
    """Sample catalogs and <=10001 rows from each pending set, with fixed codes.

    UNKNOWN retains this component's previous alarms so a consumer that emits
    set-difference recovery notifications cannot mistake failed reads for repair.
    The caller persists/delivers the returned snapshot; this function writes none.
    """
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("QUEUE_STORAGE_CLOCK_INVALID")
    result = _base(now_ms)
    old = previous if isinstance(previous, dict) else {}
    try:
        relations, control, triggers, counted, failure = _read(connect)
        result["relations"] = relations
        result["relations_observed_at_ms"] = now_ms
        result["relations_status"] = "CURRENT"
        if failure:
            result.update(
                reason=failure,
                active=sorted(_old_codes(old) | {UNAVAILABLE, PROJECTION}),
            )
            if isinstance(old.get("relations"), dict):
                result.update(
                    catalog_relations=relations,
                    relations=old["relations"],
                    relations_status="STALE",
                    relations_observed_at_ms=old.get("relations_observed_at_ms"),
                )
            return result
        projection = _projection(control, triggers, now_ms)
        old_relations = old.get("relations", {})
        old_relations = old_relations if isinstance(old_relations, dict) else {}
        vacuum = {
            name: _vacuum(relations[name], old_relations.get(name), now_ms)
            for name in ("v2_strategy_tasks", "v2_strategy_ready")
        }
        ready = _index(counted, relations["v2_strategy_ready"]["index_bytes"])
        integrity = _integrity(counted)
        result.update(
            projection=projection,
            vacuum=vacuum,
            ready=ready,
            projection_integrity=integrity,
        )
        active = set()
        if projection["status"] != "AVAILABLE":
            active.add(PROJECTION)
        if ready["status"] == "DEGRADED":
            active.add(INDEX_PRESSURE)
        if any(item["status"] == "DEGRADED" for item in vacuum.values()):
            active.add(VACUUM_PRESSURE)
        if integrity.get("missing_ready", 0) or integrity.get("unexpected_ready", 0):
            active.add(MISMATCH)
        components = [projection, ready, integrity, *vacuum.values()]
        if any(item["status"] == "UNKNOWN" for item in components):
            active |= _old_codes(old) | {UNAVAILABLE}
            result["status"] = "UNKNOWN"
        else:
            result["status"] = "DEGRADED" if active else "AVAILABLE"
        result["active"] = sorted(active)
    except Exception as exc:  # noqa: BLE001 - fixed codes only; no SQL/error text or false recovery
        if "relations" not in result and isinstance(old.get("relations"), dict):
            result.update(
                relations=old["relations"],
                relations_status="STALE",
                relations_observed_at_ms=old.get("relations_observed_at_ms"),
            )
        result.update(
            status="UNKNOWN",
            active=sorted(_old_codes(old) | {UNAVAILABLE}),
            reason="QUERY_TIMEOUT"
            if isinstance(exc, QueryCanceled)
            else "LOCK_TIMEOUT"
            if isinstance(exc, LockNotAvailable)
            else "READ_FAILED",
        )
    return result
