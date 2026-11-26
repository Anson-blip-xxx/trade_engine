-- Upgrade an EXISTING guard role before opting into MANAGEMENT_WITH_SETTLEMENT.
-- Run as the database owner; grants metadata only, never credentials or money.
GRANT SELECT(exchange,account_id,product)
ON trade_v2.v2_tenant_accounts TO trade_v2_execution_guard;
