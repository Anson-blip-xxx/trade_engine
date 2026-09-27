-- Apply once as the deployment administrator after the routing migrations.
-- The child enters this noninherited role only during fencing transactions.
-- UPDATE of one non-activation column is required by PostgreSQL FOR SHARE;
-- this does not grant UPDATE on route phase/token/epoch or account credentials.
CREATE ROLE trade_v2_execution_guard NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT USAGE ON SCHEMA trade_v2 TO trade_v2_execution_guard;
GRANT SELECT(tenant_id,environment,target_registry,binding_version,epoch,worker_token,phase) ON trade_v2.v2_execution_routes TO trade_v2_execution_guard;
GRANT UPDATE(blockers) ON trade_v2.v2_execution_routes TO trade_v2_execution_guard;
GRANT SELECT(tenant_id,registry_id,environment,version) ON trade_v2.v2_tenant_accounts TO trade_v2_execution_guard;
GRANT UPDATE(version) ON trade_v2.v2_tenant_accounts TO trade_v2_execution_guard;
GRANT SELECT(tenant_id,registry_id) ON trade_v2.v2_account_retirements TO trade_v2_execution_guard;
GRANT trade_v2_execution_guard TO ubuntu WITH INHERIT FALSE;
