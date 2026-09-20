-- Independent V2 schema; isolated QA only until cutover.
CREATE TABLE v2_trade_intents (
    intent_id UUID PRIMARY KEY,
    exchange TEXT NOT NULL,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    product TEXT NOT NULL,
    producer TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest TEXT NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (status IN (
        'RECEIVED','REJECTED','EXPIRED','EXECUTING','UNKNOWN',
        'CANCELLED','PARTIALLY_FILLED','FILLED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (exchange, account_id, environment, product, producer, request_key)
);

CREATE TABLE v2_domain_outbox (
    event_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL REFERENCES v2_trade_intents(intent_id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (intent_id, event_type)
);
-- Consumer checkpoints, execution fencing and fill accounting are later slices.
-- No worker may submit orders from this table yet.
