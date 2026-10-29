-- Offline/new-install form. Use the CONCURRENTLY deployment file online.
CREATE INDEX IF NOT EXISTS v2_signals_live_discovery
ON v2_inbound_signals(source,environment,((snapshot->>'expires_at_ms')::bigint))
INCLUDE(signal_id);
