-- Autocommit only. Do not drop an existing invalid index without inspection.
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_signals_live_discovery
ON trade_v2.v2_inbound_signals(source,environment,((snapshot->>'expires_at_ms')::bigint))
INCLUDE(signal_id);
