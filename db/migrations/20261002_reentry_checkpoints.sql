-- Derived structural state only: no order/cash/position authority.
CREATE TABLE v2_reentry_checkpoints (
    checkpoint_id TEXT PRIMARY KEY CHECK (checkpoint_id ~ '^[0-9a-f]{64}$'),
    scope JSONB NOT NULL CHECK (scope->>'environment'='SANDBOX'),
    identity JSONB NOT NULL,
    version BIGINT NOT NULL CHECK (version>0),
    through_ms BIGINT NOT NULL CHECK (through_ms>0),
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (payload->>'version'='reentry-checkpoint-v1'),
    CHECK (payload->>'execution_authorized'='false'),
    CHECK ((payload->>'through_ms')::bigint=through_ms),
    CHECK (payload->'identity'=identity),
    CHECK (octet_length(payload::text)<=131072)
);
CREATE INDEX v2_reentry_checkpoints_updated ON v2_reentry_checkpoints(updated_at);

-- Fair scheduling metadata, including failures: one bad symbol cannot starve others.
CREATE TABLE v2_reentry_preparation_attempts (
    scope_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    scope JSONB NOT NULL CHECK (scope->>'environment'='SANDBOX'),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    outcome TEXT NOT NULL,
    PRIMARY KEY(scope_key,symbol)
);
