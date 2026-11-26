-- Non-authoritative seek hints, not business evidence or completed receipts.
-- One mutable row per instance/source. Loss only restarts a bounded sweep.
CREATE TABLE v2_entry_discovery_cursors (
    consumer TEXT NOT NULL CHECK (length(consumer) BETWEEN 1 AND 1024),
    source TEXT NOT NULL CHECK (source IN ('s2','s3','tv_bridge','quant')),
    environment TEXT NOT NULL CHECK (environment='SANDBOX'),
    request_cursor TEXT CHECK (length(request_cursor) BETWEEN 1 AND 4096),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer,source,environment)
);
