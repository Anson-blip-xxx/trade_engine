"""Bounded queue observation, never a scan of retained signal history.

The validated ready projection covers unfinished canonical tasks of every age.
The recent inbox covers inputs not discovered yet. Older undiscovered inputs
remain the explicit responsibility of cyclic cold maintenance; a hot empty
result is not proof that all retained historical inputs were consumed.
"""

import time
from uuid import UUID

_READY_VALID = """SELECT c.enabled AND c.validated_at IS NOT NULL AND EXISTS (
    SELECT FROM pg_trigger g JOIN pg_proc f ON f.oid=g.tgfoid
    WHERE g.tgrelid='v2_strategy_tasks'::regclass
    AND g.tgname='v2_strategy_ready_sync' AND g.tgenabled='A'
    AND g.tgtype=29 AND g.tgqual IS NULL AND g.tgnargs=0 AND g.tgattr=''
    AND NOT g.tgisinternal AND f.proname='v2_strategy_ready_sync_row'
    AND f.pronamespace=(SELECT relnamespace FROM pg_class
        WHERE oid='v2_strategy_tasks'::regclass))
    FROM v2_strategy_ready_control c WHERE c.singleton"""

QUEUE_SQL = """WITH ready_keys AS MATERIALIZED (
    SELECT signal_id FROM v2_strategy_ready
    WHERE consumer=%s ORDER BY signal_id LIMIT %s
), recent_keys AS MATERIALIZED (
    SELECT signal_id FROM v2_inbound_signals
    WHERE environment=%s AND source=%s
    AND received_at>=to_timestamp(%s/1000.0)
    AND received_at<=to_timestamp(%s/1000.0)
    ORDER BY received_at DESC LIMIT %s
), candidate_keys AS MATERIALIZED (
    SELECT signal_id FROM ready_keys UNION SELECT signal_id FROM recent_keys
    UNION SELECT unnest(%s::uuid[])
), pending AS MATERIALIZED (
    SELECT s.signal_id,s.received_at,s.snapshot,t.error_code,t.lease_until,t.next_attempt_at,
        (s.snapshot->>'signal'=ANY(%s::text[])
         OR s.signal_id=ANY(%s::uuid[])
         OR t.error_code IS NOT NULL
         OR EXISTS (SELECT 1 FROM v2_strategy_decisions d
             WHERE d.consumer=%s AND d.signal_id=s.signal_id OFFSET 0)) AS actionable
    FROM candidate_keys p CROSS JOIN LATERAL (
        SELECT i.signal_id,i.received_at,i.snapshot,i.source,i.environment
        FROM v2_inbound_signals i WHERE i.signal_id=p.signal_id OFFSET 0) s
    LEFT JOIN LATERAL (
        SELECT t.error_code,t.lease_until,t.next_attempt_at FROM v2_strategy_tasks t
        WHERE t.signal_id=s.signal_id AND t.consumer=%s OFFSET 0) t ON true
    WHERE s.environment=%s AND s.source=%s
    AND (s.received_at<=to_timestamp(%s/1000.0) OR s.signal_id=ANY(%s::uuid[]))
    AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
        WHERE r.consumer=%s AND r.signal_id=s.signal_id OFFSET 0)
)
SELECT count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)),
    min(floor(extract(epoch FROM s.received_at)*1000)::bigint)
        FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)),
    count(*) FILTER(WHERE error_code=ANY(%s)),
    count(*) FILTER(WHERE NOT actionable),
    count(*) FILTER(WHERE actionable AND (snapshot->>'expires_at_ms')::bigint>%s),
    count(*) FILTER(WHERE actionable AND (snapshot->>'expires_at_ms')::bigint<=%s),
    count(*) FILTER(WHERE actionable AND lease_until>=statement_timestamp()),
    count(*) FILTER(WHERE actionable AND next_attempt_at>statement_timestamp()
        AND (lease_until IS NULL OR lease_until<statement_timestamp())),
    count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)
        AND (snapshot->>'expires_at_ms')::bigint>%s),
    count(*) FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)
        AND (snapshot->>'expires_at_ms')::bigint<=%s),
    count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
    count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
    count(*) FILTER(WHERE actionable AND error_code=ANY(%s)),
    COALESCE((SELECT jsonb_object_agg(reason_code,reason_count) FROM (
        SELECT CASE WHEN error_code=ANY(%s) THEN error_code
            ELSE 'UNCLASSIFIED_TASK_ERROR' END AS reason_code,
            count(*) AS reason_count FROM pending
        WHERE actionable AND error_code IS NOT NULL GROUP BY 1
    ) reasons),'{}'::jsonb),
    (SELECT count(*) FROM ready_keys),
    (SELECT count(*) FROM recent_keys),
    EXISTS (SELECT FROM ready_keys q LEFT JOIN LATERAL (
        SELECT signal_id,completed_at FROM v2_strategy_tasks t
        WHERE t.consumer=%s AND t.signal_id=q.signal_id OFFSET 0) t ON true
        WHERE t.signal_id IS NULL OR t.completed_at IS NOT NULL),
    COALESCE(array_agg(s.signal_id ORDER BY s.signal_id)
        FILTER(WHERE actionable AND COALESCE(error_code,'')<>ALL(%s)),'{}'::uuid[])
FROM pending s"""


def read_signal_queue(
    connect,
    *,
    environment,
    source,
    consumer,
    now_ms,
    queue_seconds,
    reconcile_seconds,
    actionable_events,
    expected_waits,
    admission_quota_codes,
    capacity_codes,
    dependency_codes,
    reason_codes,
    previous_signal_ids=(),
    limit=1000,
    budget_ms=1000,
):
    """Return the legacy 14 metrics plus honest bounded coverage diagnostics.

    UNKNOWN may contain lower-bound positive counts if a cap was reached, but
    it must never authorize clearing an old lag alarm or be presented as zero.
    Each query is nonparallel; all expensive work is limited before anti-joins.
    """
    for value, lower, upper in (
        (now_ms, 0, 2**63 - 1),
        (queue_seconds, 1, 3600),
        (reconcile_seconds, 1, 3600),
        (limit, 1, 10000),
        (budget_ms, 1, 5000),
    ):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("QUEUE_OBSERVATION_BOUND_INVALID")
    started = time.perf_counter()
    lower = now_ms - max(reconcile_seconds, 2 * queue_seconds) * 1000
    cutoff = now_ms - queue_seconds * 1000
    diagnostics = {
        "status": "UNKNOWN",
        "scope": "VALIDATED_READY_AND_RECENT_INBOX",
        "historical_undiscovered_coverage": "COLD_MAINTENANCE_ONLY",
        "recent_from_ms": lower,
        "recent_to_ms": cutoff,
        "limit_per_source": limit,
        "budget_ms": budget_ms,
        "lag_signal_ids": [],
    }

    def result(row, reason):
        diagnostics["reason"] = reason
        diagnostics["query_ms"] = round((time.perf_counter() - started) * 1000)
        return {"row": row, "diagnostics": diagnostics}

    tracking_limit = 2 * limit + 2
    if (
        not isinstance(previous_signal_ids, (tuple, list))
        or len(previous_signal_ids) > tracking_limit
    ):
        return result(None, "QUEUE_TRACKING_KEYS_INVALID")
    try:
        if any(type(value) is not str for value in previous_signal_ids):
            return result(None, "QUEUE_TRACKING_KEYS_INVALID")
        previous_signal_ids = tuple(
            sorted({str(UUID(value)) for value in previous_signal_ids})
        )
    except (ValueError, AttributeError):
        return result(None, "QUEUE_TRACKING_KEYS_INVALID")
    # Unknown observations must retain known IDs; callers must also retain their
    # last diagnostics on malformed legacy data. Only a complete read may prune
    # a tracked ID after rechecking its own immutable receipt / expected wait.
    diagnostics["lag_signal_ids"] = list(previous_signal_ids)

    def budget(conn):
        remaining = budget_ms - int((time.perf_counter() - started) * 1000)
        if remaining <= 0:
            raise TimeoutError("QUEUE_OBSERVATION_BUDGET_EXHAUSTED")
        conn.execute(
            "SELECT set_config('statement_timeout',%s,true), "
            "set_config('lock_timeout',%s,true)",
            (f"{remaining}ms", f"{min(remaining, 50)}ms"),
        )

    try:
        with connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL max_parallel_workers_per_gather=0")
            conn.execute("SET LOCAL jit=off")
            budget(conn)
            relations = conn.execute(
                "SELECT to_regclass('v2_strategy_ready_control'),"
                "to_regclass('v2_strategy_ready')"
            ).fetchone()
            if relations is None or any(value is None for value in relations):
                return result(None, "READY_SCHEMA_ABSENT")
            budget(conn)
            if conn.execute(_READY_VALID).fetchone() != (True,):
                return result(None, "READY_NOT_ENABLED_OR_TRIGGER_INVALID")
            budget(conn)
            row = conn.execute(
                QUEUE_SQL,
                (
                    consumer,
                    limit + 1,
                    environment,
                    source,
                    lower,
                    cutoff,
                    limit + 1,
                    list(previous_signal_ids),
                    list(actionable_events),
                    list(previous_signal_ids),
                    consumer,
                    consumer,
                    environment,
                    source,
                    cutoff,
                    list(previous_signal_ids),
                    consumer,
                    sorted(expected_waits),
                    sorted(expected_waits),
                    sorted(expected_waits),
                    now_ms,
                    now_ms,
                    sorted(expected_waits),
                    now_ms,
                    sorted(expected_waits),
                    now_ms,
                    sorted(admission_quota_codes),
                    sorted(capacity_codes),
                    sorted(dependency_codes),
                    sorted(reason_codes),
                    consumer,
                    sorted(expected_waits),
                ),
            ).fetchone()
        if row is None or len(row) != 18:
            return result(None, "QUEUE_RESULT_INVALID")
        diagnostics.update(ready_keys=row[14], recent_keys=row[15])
        if row[16]:
            return result(None, "READY_CANONICAL_MISMATCH")
        lag_ids = sorted({str(value) for value in row[17]})
        retained = list(previous_signal_ids)
        previous_set = set(previous_signal_ids)
        retained.extend(value for value in lag_ids if value not in previous_set)
        if row[14] > limit or row[15] > limit:
            # The recent window will move past keys which this capped read
            # never saw. Unlike ready tasks, those undiscovered keys need not
            # remain in a persistent projection. Mark the lost identity
            # coverage even when every sampled lag ID fits the tracking cap;
            # a later empty hot read cannot prove those unseen inputs drained.
            if row[15] > limit:
                diagnostics["tracking_overflow"] = True
                diagnostics["coverage_gap_reason"] = "RECENT_INBOX_WINDOW_LIMIT_REACHED"
            elif len(retained) > tracking_limit:
                diagnostics["tracking_overflow"] = True
            diagnostics["lag_signal_ids"] = retained[:tracking_limit]
            return result(tuple(row[:14]), "QUEUE_OBSERVATION_LIMIT_REACHED")
        if len(lag_ids) > tracking_limit:
            diagnostics["tracking_overflow"] = True
            diagnostics["lag_signal_ids"] = retained[:tracking_limit]
            return result(tuple(row[:14]), "QUEUE_TRACKING_LIMIT_REACHED")
        diagnostics["lag_signal_ids"] = lag_ids
        diagnostics["status"] = "COMPLETE"
        return result(tuple(row[:14]), "BOUNDED_HOT_QUEUE_OBSERVED")
    except Exception as exc:  # noqa: BLE001 - no SQL, identity or exception text
        diagnostics["failure_type"] = type(exc).__name__
        return result(None, "QUEUE_OBSERVATION_UNAVAILABLE")
