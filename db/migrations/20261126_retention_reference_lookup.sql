-- After postgres_v2_core_schema.sql creates v2_s3_frames. This is a lookup
-- index only: no retention policy, protected reference, business row or grant
-- changes. Offline/isolated-schema migration; use the separate CONCURRENTLY
-- deployment script for an existing populated database.
CREATE INDEX IF NOT EXISTS v2_s3_frames_state_reference
  ON v2_s3_frames(state_id,state_version);

-- IF NOT EXISTS alone accepts an invalid or differently defined index. Fail
-- closed instead; never drop or rebuild an existing object automatically.
DO $retention_reference_check$
DECLARE
    target_table regclass := 'v2_s3_frames'::regclass;
    index_oid oid;
BEGIN
    SELECT c.oid INTO index_oid FROM pg_class c
    WHERE c.relnamespace=(SELECT relnamespace FROM pg_class WHERE oid=target_table)
      AND c.relname='v2_s3_frames_state_reference';
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
