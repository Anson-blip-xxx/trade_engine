-- Trusted non-Web controller. The Web role retains request-only rights.
CREATE ROLE trade_v2_execution_operator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA trade_v2 TO trade_v2_execution_operator;
GRANT SELECT, INSERT, UPDATE ON trade_v2.v2_execution_routes TO trade_v2_execution_operator;
GRANT SELECT ON trade_v2.v2_execution_route_events,trade_v2.v2_account_retirements,trade_v2.v2_tenant_accounts TO trade_v2_execution_operator;
GRANT UPDATE(version) ON trade_v2.v2_tenant_accounts TO trade_v2_execution_operator;
GRANT SELECT, INSERT ON trade_v2.v2_execution_controller_events TO trade_v2_execution_operator;
GRANT trade_v2_execution_operator TO ubuntu WITH INHERIT FALSE;
-- Guarded child workers need SHARE row locks, not route activation rights.
GRANT SELECT ON trade_v2.v2_execution_routes TO ubuntu;
GRANT UPDATE(blockers) ON trade_v2.v2_execution_routes TO ubuntu;
GRANT SELECT ON trade_v2.v2_execution_controller_events TO tradev2accounts;
