-- Additive typed cold-history seek. This is NOT an execution authority/index.
-- On an existing populated production inbox, create this same index with
-- CREATE INDEX CONCURRENTLY outside a transaction, then verify indisvalid.
-- The transactional spelling here is for fresh schemas and isolated QA.
-- Existing v2_signals_live_discovery remains in use by independent hot work.
CREATE INDEX v2_signals_typed_history_seek
ON v2_inbound_signals(source,environment,((snapshot->>'expires_at_ms')::bigint),signal_id)
INCLUDE(request_key);
