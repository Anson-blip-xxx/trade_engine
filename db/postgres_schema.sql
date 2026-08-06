-- Transactional source of truth for Binance position-level accounting.
-- Run with: psql "$POSTGRES_DSN" -f db/postgres_schema.sql

CREATE TABLE IF NOT EXISTS trade_episodes (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    system_name TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_price DOUBLE PRECISION NOT NULL,
    qty DOUBLE PRECISION NOT NULL CHECK (qty > 0),
    leverage INTEGER NOT NULL DEFAULT 1,
    pnl_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    pnl_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
    duration_min INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL,
    exit_reason TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    strength DOUBLE PRECISION NOT NULL DEFAULT 0,
    margin_mode TEXT NOT NULL DEFAULT '',
    sl_price DOUBLE PRECISION NOT NULL DEFAULT 0,
    ghost_cleanup BOOLEAN NOT NULL DEFAULT FALSE,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS trade_episodes_closed_at_idx
    ON trade_episodes (closed_at);
CREATE INDEX IF NOT EXISTS trade_episodes_symbol_idx
    ON trade_episodes (symbol, closed_at);

CREATE TABLE IF NOT EXISTS trade_events (
    event_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    order_id TEXT NOT NULL DEFAULT '',
    fill_id TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL DEFAULT 0,
    qty DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS trade_events_fill_idx
    ON trade_events (fill_id)
    WHERE fill_id <> '';
