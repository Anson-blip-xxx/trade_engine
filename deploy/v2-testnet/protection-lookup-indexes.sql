-- Run using psql outside a transaction on trade_v2_testnet only.
-- Concurrent builds do not block account/ledger writes. No business row changes.
-- Inspect indisvalid/indisready after building; IF NOT EXISTS does not repair an
-- interrupted invalid index. Never silently drop an existing index to retry.
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_protection_active_states
  ON trade_v2.v2_business_state(state_id)
  WHERE NOT deleted AND scope->>'namespace'='testnet-protection-v1';
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_protection_recovery_keys
  ON trade_v2.v2_business_state((scope->>'key'))
  WHERE NOT deleted AND scope->>'namespace'='protection-recovery-result-v1';
