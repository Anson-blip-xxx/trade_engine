"""Owner-only, transaction-local compaction of duplicate coverage current rows.

The immutable history payload is NEVER updated or deleted. Other namespaces,
versions, identities, and ledgers remain untouched. Commit each bounded batch;
on any error rollback the whole transaction, including trigger DDL.
"""

from uuid import UUID

from psycopg import sql


def compact_legacy_batch(conn, state_ids):
    if not isinstance(state_ids, (tuple, list)) or not 1 <= len(state_ids) <= 100:
        raise ValueError("COMPACTION_BATCH_BOUND")
    ids = [str(UUID(value)) for value in state_ids]
    if len(ids) != len(set(ids)):
        raise ValueError("COMPACTION_DUPLICATE_ID")
    if conn.autocommit or conn.execute("SHOW is_superuser").fetchone()[0] != "on":
        raise ValueError("COMPACTION_OWNER_TRANSACTION_REQUIRED")
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    table = sql.Identifier(schema, "v2_business_state")
    history = sql.Identifier(schema, "v2_state_history")
    conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(table))
    valid = conn.execute(
        # Materialize an uncompressed object once. Equality and shape checks
        # otherwise decompress the same large TOAST payload repeatedly.
        # Caller bounds work_mem/temp spill and batch size for available RAM.
        sql.SQL("""WITH selected AS MATERIALIZED (
        SELECT state_id,version,deleted,payload || '{{}}'::jsonb AS payload
        FROM {} WHERE state_id=ANY(%s::uuid[])
          AND scope->>'namespace'='account-coverage-evidence-v1'
        ) SELECT b.state_id::text FROM selected b JOIN {} h
        ON h.state_id=b.state_id AND h.version=b.version
        WHERE b.version=1 AND NOT b.deleted AND NOT h.deleted
          AND h.reason='ACCOUNT_COVERAGE_AUDIT' AND b.payload=h.payload
          AND b.payload ?& ARRAY['inventory','before','after','result']
          AND NOT b.payload ? '_v2_state_history_ref'""").format(table, history),
        (ids,),
    ).fetchall()
    if {row[0] for row in valid} != set(ids):
        raise ValueError("COMPACTION_EVIDENCE_MISMATCH")
    # This transaction's ACCESS EXCLUSIVE lock fences every other state writer.
    # The only bypass is representation, not semantic version or source history.
    conn.execute(
        sql.SQL("ALTER TABLE {} DISABLE TRIGGER v2_state_version").format(table)
    )
    updated = conn.execute(
        sql.SQL("""UPDATE {} SET payload=jsonb_build_object(
            '_v2_state_history_ref',state_id::text,'version',version)
            WHERE state_id=ANY(%s::uuid[])""").format(table),
        (ids,),
    ).rowcount
    conn.execute(
        sql.SQL("ALTER TABLE {} ENABLE TRIGGER v2_state_version").format(table)
    )
    if updated != len(ids):
        raise ValueError("COMPACTION_ROW_COUNT_CHANGED")
    return updated
