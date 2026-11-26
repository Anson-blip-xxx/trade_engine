-- Disposable seek hints only, never evidence of completion or zero exposure.
-- Checkpoint before each attempt, wrap after a bounded short page. Loss merely
-- restarts discovery; the original signal receipt / order journals stay truth.
CREATE TABLE v2_strategy_maintenance_cursors (
    consumer TEXT NOT NULL CHECK (length(consumer) BETWEEN 1 AND 1024),
    source TEXT NOT NULL CHECK (source IN ('s2','s3','tv_bridge','quant')),
    environment TEXT NOT NULL CHECK (environment='SANDBOX'),
    request_cursor TEXT CHECK (length(request_cursor) BETWEEN 1 AND 4096),
    intent_cursor UUID,
    next_lane TEXT NOT NULL DEFAULT 'signals' CHECK (next_lane IN ('signals','cleanup')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer,source,environment)
);
