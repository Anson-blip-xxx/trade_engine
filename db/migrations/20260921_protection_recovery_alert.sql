-- Execute in one transaction with the explicitly selected V2 search_path.
-- Additive event contract; no history deletion and no service restart required.
SET LOCAL lock_timeout TO '5s';
ALTER TABLE v2_operational_outbox
    DROP CONSTRAINT v2_operational_outbox_event_type_check,
    ADD CONSTRAINT v2_operational_outbox_event_type_check
    CHECK (event_type IN ('MARKET_FAILURE','CANDLE_QUARANTINED','ACCOUNT_INVENTORY','PROTECTION_RECOVERY'));
