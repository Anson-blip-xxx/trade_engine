-- Explicit operator deployment only, using psql on trade_v2_testnet outside a
-- transaction. This script does not enable a service or delete any rows.
-- Existing invalid/wrong-definition indexes require investigation; never drop
-- one automatically. Interrupted concurrent builds may leave an invalid index.
-- A successful build is NOT production latency acceptance: recheck the actual
-- bounded retention EXPLAIN and 5-second job result after deployment, without
-- relaxing reference protection, the batch cap or its timeout.
\set ON_ERROR_STOP on

DO $retention_reference_database$
BEGIN
    IF current_database()<>'trade_v2_testnet' THEN
        RAISE EXCEPTION 'RETENTION_REFERENCE_DATABASE_REQUIRED';
    END IF;
END;
$retention_reference_database$;

-- Per statement/wait budgets, not a promise about total script wall time.
-- A canceled concurrent build must be inspected manually before retrying.
SET lock_timeout='5s';
SET statement_timeout='15min';
SET maintenance_work_mem='64MB';
SET work_mem='4MB';
SET max_parallel_maintenance_workers=0;
SET max_parallel_workers_per_gather=0;

-- Preflight rejects a conflicting existing object before attempting a build.
DO $retention_reference_check$
DECLARE
    target_table regclass := 'trade_v2.v2_s3_frames'::regclass;
    index_oid oid := to_regclass('trade_v2.v2_s3_frames_state_reference');
BEGIN
    IF index_oid IS NOT NULL AND NOT EXISTS (
        SELECT FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_am a ON a.oid=c.relam
        WHERE i.indexrelid=index_oid AND i.indrelid=target_table
          AND c.relkind='i' AND a.amname='btree'
          AND i.indisvalid AND i.indisready AND i.indislive
          AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
          AND i.indnkeyatts=2 AND i.indnatts=2
          AND i.indoption::text='0 0' AND i.indcollation::text='0 0'
          AND i.indclass[0]=(SELECT oid FROM pg_opclass
              WHERE opcnamespace='pg_catalog'::regnamespace AND opcname='uuid_ops'
                AND opcmethod=c.relam AND opcdefault AND opcintype='uuid'::regtype)
          AND i.indclass[1]=(SELECT oid FROM pg_opclass
              WHERE opcnamespace='pg_catalog'::regnamespace AND opcname='int8_ops'
                AND opcmethod=c.relam AND opcdefault AND opcintype='bigint'::regtype)
          AND i.indexprs IS NULL AND i.indpred IS NULL
          AND pg_get_indexdef(index_oid,1,true)='state_id'
          AND pg_get_indexdef(index_oid,2,true)='state_version'
    ) THEN
        RAISE EXCEPTION 'RETENTION_REFERENCE_INDEX_INVALID';
    END IF;
END;
$retention_reference_check$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_s3_frames_state_reference
  ON trade_v2.v2_s3_frames(state_id,state_version);

-- A completed statement is not enough: require the exact ready/valid index.
DO $retention_reference_check$
DECLARE
    target_table regclass := 'trade_v2.v2_s3_frames'::regclass;
    index_oid oid := to_regclass('trade_v2.v2_s3_frames_state_reference');
BEGIN
    IF index_oid IS NULL OR NOT EXISTS (
        SELECT FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        JOIN pg_am a ON a.oid=c.relam
        WHERE i.indexrelid=index_oid AND i.indrelid=target_table
          AND c.relkind='i' AND a.amname='btree'
          AND i.indisvalid AND i.indisready AND i.indislive
          AND NOT i.indisunique AND NOT i.indisprimary AND NOT i.indisexclusion
          AND i.indnkeyatts=2 AND i.indnatts=2
          AND i.indoption::text='0 0' AND i.indcollation::text='0 0'
          AND i.indclass[0]=(SELECT oid FROM pg_opclass
              WHERE opcnamespace='pg_catalog'::regnamespace AND opcname='uuid_ops'
                AND opcmethod=c.relam AND opcdefault AND opcintype='uuid'::regtype)
          AND i.indclass[1]=(SELECT oid FROM pg_opclass
              WHERE opcnamespace='pg_catalog'::regnamespace AND opcname='int8_ops'
                AND opcmethod=c.relam AND opcdefault AND opcintype='bigint'::regtype)
          AND i.indexprs IS NULL AND i.indpred IS NULL
          AND pg_get_indexdef(index_oid,1,true)='state_id'
          AND pg_get_indexdef(index_oid,2,true)='state_version'
    ) THEN
        RAISE EXCEPTION 'RETENTION_REFERENCE_INDEX_INVALID';
    END IF;
END;
$retention_reference_check$;
