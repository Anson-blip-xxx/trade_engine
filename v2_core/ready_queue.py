"""Optional hot task projection; original tasks and leases remain authoritative."""

from psycopg import Error


def queue_mode(connect):
    """Fail back to the original canonical query, never infer an empty queue.

    The enable function verifies set equality under table locks once backfill
    finishes. Transactional triggers maintain that invariant thereafter. A
    missing/unready migration, disabled trigger or failed read must not hide
    canonical tasks. This read is advisory and never grants order authority.
    """
    try:
        with connect() as conn:
            conn.execute("SET LOCAL statement_timeout='200ms'")
            conn.execute("SET LOCAL lock_timeout='50ms'")
            relations = conn.execute(
                "SELECT to_regclass('v2_strategy_ready_control'),"
                "to_regclass('v2_strategy_ready')"
            ).fetchone()
            if relations is None or any(item is None for item in relations):
                return "CANONICAL", "READY_SCHEMA_ABSENT"
            row = conn.execute(
                """SELECT c.enabled AND c.validated_at IS NOT NULL AND EXISTS (
                    SELECT FROM pg_trigger g JOIN pg_proc f ON f.oid=g.tgfoid
                    WHERE g.tgrelid='v2_strategy_tasks'::regclass
                    AND g.tgname='v2_strategy_ready_sync' AND g.tgenabled='A'
                    AND g.tgtype=29 AND g.tgqual IS NULL AND g.tgnargs=0 AND g.tgattr=''
                    AND NOT g.tgisinternal AND f.proname='v2_strategy_ready_sync_row'
                    AND f.pronamespace=(SELECT relnamespace FROM pg_class
                        WHERE oid='v2_strategy_tasks'::regclass))
                FROM v2_strategy_ready_control c WHERE c.singleton"""
            ).fetchone()
        return (
            ("READY", "VALIDATED_TRANSACTIONAL_PROJECTION")
            if row == (True,)
            else ("CANONICAL", "READY_NOT_ENABLED_OR_TRIGGER_INVALID")
        )
    except Error:
        # The failed transaction has rolled back before using the original
        # query. Do not soften claim/ACK errors or change task leases.
        return "CANONICAL", "READY_CHECK_UNAVAILABLE"


READY_CTE = """ready AS MATERIALIZED (
    SELECT consumer,signal_id FROM v2_strategy_ready WHERE consumer=%s),
    candidate_keys AS MATERIALIZED (
        SELECT original.consumer,original.signal_id,original.completed_at
        FROM ready q CROSS JOIN LATERAL (
            SELECT task.* FROM v2_strategy_tasks task
            WHERE task.consumer=q.consumer AND task.signal_id=q.signal_id
            OFFSET 0) original
        WHERE original.completed_at IS NULL
        AND original.next_attempt_at<=clock_timestamp()
        AND (original.lease_until IS NULL OR original.lease_until<clock_timestamp())
    ),"""
# The LATERAL probe above is read-only. Lock only the final base relation t,
# after ranking and LIMIT; locking the probe alias would lock every candidate.
READY_SOURCE = """candidate_keys q JOIN v2_strategy_tasks t
    ON t.consumer=q.consumer AND t.signal_id=q.signal_id"""
# Every materialized candidate has completed_at=NULL. Null-safe equality both
# rechecks the canonical row after a concurrent change and keeps this final
# point lookup from turning into a scan of the historical partial due index.
READY_COMPLETION_CHECK = "t.completed_at IS NOT DISTINCT FROM q.completed_at"
