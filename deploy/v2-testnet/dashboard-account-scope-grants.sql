-- Read-only dashboard context. No credentials, worker tokens or mutation grants.
GRANT SELECT(tenant_id,environment,epoch,target_registry,phase,blockers)
  ON trade_v2.v2_execution_routes TO tradev2dash;
GRANT SELECT(tenant_id,registry_id,account_id,environment,exchange,product)
  ON trade_v2.v2_tenant_accounts TO tradev2dash;
GRANT SELECT(tenant_id,registry_id,alias)
  ON trade_v2.v2_account_aliases TO tradev2dash;
