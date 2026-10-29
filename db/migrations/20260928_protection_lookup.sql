-- Offline/isolated migration. For running services use the concurrent deployment file.
-- Narrow partial indexes avoid indexing arbitrary (potentially long) state keys.
CREATE INDEX IF NOT EXISTS v2_protection_active_states
  ON v2_business_state(state_id)
  WHERE NOT deleted AND scope->>'namespace'='testnet-protection-v1';
CREATE INDEX IF NOT EXISTS v2_protection_recovery_keys
  ON v2_business_state((scope->>'key'))
  WHERE NOT deleted AND scope->>'namespace'='protection-recovery-result-v1';
