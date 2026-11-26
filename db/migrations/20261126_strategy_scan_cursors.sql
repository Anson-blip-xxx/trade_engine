-- Disposable progress only. Signals/opportunity receipts remain authoritative.
CREATE TABLE IF NOT EXISTS v2_strategy_scan_cursors (
    consumer text PRIMARY KEY,
    window_ms bigint NOT NULL CHECK (window_ms >= 0 AND window_ms % 900000 = 0),
    after_symbol text NOT NULL,
    completed boolean NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
