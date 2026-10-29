-- Outside a transaction. Keep the age lookup from scanning the large TOAST table.
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_history_diagnostic_retention_age
ON trade_v2.v2_state_history(created_at,event_id) INCLUDE(state_id,version)
WHERE reason IN ('HOST_DISK_ASSESSMENT','TESTNET_TRADING_DAEMON_CYCLE','TRADING_BUSINESS_HEALTH','TRADING_PIPELINE_CYCLE');
