-- Additive event vocabulary: no history rewrites, no trading/account migrations.
ALTER TABLE v2_operational_outbox DROP CONSTRAINT v2_operational_outbox_event_type_check;
ALTER TABLE v2_operational_outbox ADD CONSTRAINT v2_operational_outbox_event_type_check
CHECK (event_type IN ('MARKET_FAILURE','MARKET_RECOVERED','CANDLE_QUARANTINED','ACCOUNT_INVENTORY','PROTECTION_RECOVERY'));
CREATE INDEX IF NOT EXISTS v2_market_incident_history ON v2_operational_outbox(scope_id,event_type,created_at,event_id);
