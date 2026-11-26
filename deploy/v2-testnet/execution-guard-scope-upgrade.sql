-- Idempotent upgrade for an existing least-privilege execution guard.
-- Apply as deployment administrator; no credential or activation privileges.
-- New installations use execution-guard-grants.sql instead.
BEGIN;
SET LOCAL lock_timeout = '2s';
GRANT SELECT(exchange, account_id, product)
  ON trade_v2.v2_tenant_accounts TO trade_v2_execution_guard;
COMMIT;
