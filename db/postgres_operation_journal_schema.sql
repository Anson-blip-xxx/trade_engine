-- Dormant V2 schema. Do not apply to production before the D3A rollout gate.
CREATE TABLE IF NOT EXISTS trade_operations (
    operation_id UUID PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    operation_type TEXT NOT NULL CHECK (operation_type IN (
        'OPEN','CLOSE_FULL','CLOSE_PARTIAL','PROTECTION_CREATE',
        'PROTECTION_REPLACE','GHOST_FINALIZE','EXTERNAL_RECONCILE')),
    slot_digest TEXT NOT NULL,
    exchange_position_key JSONB NOT NULL,
    request_id TEXT,
    position_episode_id UUID,
    lifecycle_generation BIGINT CHECK (lifecycle_generation > 0),
    protection_generation BIGINT CHECK (protection_generation > 0),
    stage TEXT NOT NULL CHECK (stage IN (
        'NEW','INTENT_DURABLE','SUBMITTING','UNKNOWN','EXCHANGE_ACKED',
        'EFFECT_CONFIRMED','LOCAL_PROJECTED','AUXILIARY_PENDING','COMPLETED',
        'FAILED_RETRYABLE','FAILED_TERMINAL','COMPENSATION_REQUIRED')),
    version BIGINT NOT NULL CHECK (version > 0),
    owner_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    input JSONB NOT NULL CHECK (jsonb_typeof(input) = 'object'),
    exchange_aliases JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(exchange_aliases) = 'object'),
    effect_summary JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(effect_summary) = 'object'),
    pending_requirements JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(pending_requirements) = 'array'),
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (updated_at >= created_at),
    CHECK ((owner_token IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX IF NOT EXISTS trade_operations_recovery_idx
    ON trade_operations (stage, next_attempt_at, updated_at)
    WHERE stage NOT IN ('COMPLETED', 'FAILED_TERMINAL');
CREATE INDEX IF NOT EXISTS trade_operations_slot_idx
    ON trade_operations (slot_digest, updated_at DESC);
CREATE INDEX IF NOT EXISTS trade_operations_request_idx
    ON trade_operations (request_id) WHERE request_id IS NOT NULL;
