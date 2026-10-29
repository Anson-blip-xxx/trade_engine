-- Additive storage only. Full immutable fact objects remain in PostgreSQL.
CREATE TABLE IF NOT EXISTS v2_coverage_blobs (
    blob_id TEXT PRIMARY KEY CHECK (blob_id ~ '^[0-9a-f]{64}$'),
    content_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (blob_id=encode(sha256(convert_to(content_json,'UTF8')),'hex')),
    CHECK (jsonb_typeof(content_json::jsonb)='object'),
    CHECK (content_json::jsonb->>'kind' IN ('EPISODE','ORDER','PROTECTION','CHUNK','MANIFEST')),
    CHECK (jsonb_typeof(content_json::jsonb->'account_scope')='object')
);
CREATE TRIGGER v2_coverage_blobs_immutable BEFORE UPDATE OR DELETE ON v2_coverage_blobs
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
