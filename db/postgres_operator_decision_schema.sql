-- Dormant V2 schema. Do not apply before the Decision Center rollout gate.
CREATE TABLE IF NOT EXISTS operator_decisions (
    decision_id UUID PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    event_id UUID NOT NULL UNIQUE,
    operation_id UUID,
    slot_digest TEXT NOT NULL,
    exchange_position_key JSONB NOT NULL
        CHECK (jsonb_typeof(exchange_position_key) = 'object'),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'RISK_INCREASING_REQUEST','UNKNOWN_MUTATION','UNPROTECTED_POSITION',
        'EXTERNAL_POSITION','OPERATOR_APPROVAL')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','HIGH','CRITICAL')),
    status TEXT NOT NULL CHECK (status IN (
        'OPEN','ACKNOWLEDGED','RESOLVED','EXPIRED','SUPERSEDED')),
    version BIGINT NOT NULL CHECK (version > 0),
    trigger_context JSONB NOT NULL CHECK (jsonb_typeof(trigger_context) = 'object'),
    current_context JSONB NOT NULL CHECK (jsonb_typeof(current_context) = 'object'),
    trigger_context_digest TEXT NOT NULL
        CHECK (trigger_context_digest ~ '^[0-9a-f]{64}$'),
    current_context_digest TEXT NOT NULL
        CHECK (current_context_digest ~ '^[0-9a-f]{64}$'),
    fallback_action TEXT NOT NULL CHECK (fallback_action IN (
        'REVALIDATE_REQUEST','CANCEL_REQUEST','QUERY_AND_RECONCILE',
        'QUARANTINE_AND_QUERY','RESTORE_PROTECTION',
        'PAUSE_OPENS_AND_RESTORE_PROTECTION',
        'QUARANTINE_AND_RESTORE_PROTECTION','EMERGENCY_REDUCE_ONLY',
        'WAIT_FOR_APPROVAL','REBUILD_DECISION')),
    fallback_at TIMESTAMPTZ NOT NULL,
    approval_expires_at TIMESTAMPTZ,
    assigned_to TEXT,
    resolution TEXT CHECK (resolution IN (
        'APPROVE','REJECT','QUARANTINE','REBUILD','AUTOMATIC_FALLBACK')),
    resolution_detail JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(resolution_detail) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (updated_at >= created_at),
    CHECK (fallback_at >= created_at),
    CHECK (approval_expires_at IS NULL OR approval_expires_at > created_at),
    CHECK ((status = 'RESOLVED') = (resolution IS NOT NULL)),
    CHECK (status = 'RESOLVED' OR resolution_detail = '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS operator_decisions_inbox_idx
    ON operator_decisions (status, severity, fallback_at, created_at);
CREATE INDEX IF NOT EXISTS operator_decisions_slot_idx
    ON operator_decisions (slot_digest, updated_at DESC);

CREATE TABLE IF NOT EXISTS operator_notification_outbox (
    notification_id UUID PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    decision_id UUID NOT NULL REFERENCES operator_decisions(decision_id),
    channel TEXT NOT NULL CHECK (channel IN ('TELEGRAM')),
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING','CLAIMED','RETRY_WAIT','DELIVERED','DEAD_LETTER')),
    version BIGINT NOT NULL CHECK (version > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL,
    owner_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (updated_at >= created_at),
    CHECK (next_attempt_at >= created_at),
    CHECK ((owner_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK ((status = 'CLAIMED') = (owner_token IS NOT NULL)),
    CHECK ((status = 'DELIVERED') = (delivered_at IS NOT NULL)),
    CHECK (delivered_at IS NULL OR
        (delivered_at >= created_at AND delivered_at <= updated_at)),
    CHECK ((status = 'PENDING' AND attempt_count = 0) OR
        (status <> 'PENDING' AND attempt_count > 0)),
    CHECK (status <> 'CLAIMED' OR lease_expires_at > updated_at),
    CHECK (status <> 'RETRY_WAIT' OR
        (last_error IS NOT NULL AND next_attempt_at > updated_at)),
    CHECK (status <> 'DEAD_LETTER' OR last_error IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS operator_notification_due_idx
    ON operator_notification_outbox (next_attempt_at, created_at)
    WHERE status IN ('PENDING','RETRY_WAIT');
CREATE INDEX IF NOT EXISTS operator_notification_expired_claim_idx
    ON operator_notification_outbox (lease_expires_at, created_at)
    WHERE status = 'CLAIMED';
CREATE INDEX IF NOT EXISTS operator_notification_decision_idx
    ON operator_notification_outbox (decision_id, created_at);
