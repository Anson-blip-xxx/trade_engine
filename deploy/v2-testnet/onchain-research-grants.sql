-- Dedicated peer-auth identity; no credentials, order writes or live-policy access.
DO $$ BEGIN
 IF current_database()<>'trade_v2_testnet' THEN RAISE EXCEPTION 'WRONG_DATABASE'; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='tradev2onchain') THEN
  CREATE ROLE tradev2onchain LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
   NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
 END IF;
 IF EXISTS(SELECT FROM pg_roles r WHERE rolname='tradev2onchain' AND
     (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls OR rolinherit
      OR EXISTS(SELECT FROM pg_auth_members m WHERE m.member=r.oid))) THEN
  RAISE EXCEPTION 'ONCHAIN_ROLE_MUST_BE_ISOLATED';
 END IF;
 IF has_table_privilege('tradev2onchain','trade_v2.v2_orders','INSERT')
    OR has_table_privilege('tradev2onchain','trade_v2.v2_orders','UPDATE')
    OR has_table_privilege('tradev2onchain','trade_v2.v2_credential_vault','SELECT') THEN
  RAISE EXCEPTION 'ONCHAIN_ROLE_HAS_FORBIDDEN_PRIVILEGE';
 END IF;
 IF EXISTS(SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname='trade_v2' AND p.prosecdef
       AND has_function_privilege('tradev2onchain',p.oid,'EXECUTE')) THEN
  RAISE EXCEPTION 'ONCHAIN_ROLE_MUST_NOT_EXECUTE_PRIVILEGED_FUNCTIONS';
 END IF;
END $$;
ALTER ROLE tradev2onchain CONNECTION LIMIT 3;
ALTER ROLE tradev2onchain SET statement_timeout='1500ms';
ALTER ROLE tradev2onchain SET lock_timeout='100ms';
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2onchain;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2onchain;
GRANT SELECT (tenant_id,registry_id,version,exchange,account_id,environment,product)
 ON trade_v2.v2_tenant_accounts TO tradev2onchain;
GRANT SELECT (tenant_id,target_registry,binding_version,environment,phase)
 ON trade_v2.v2_execution_routes TO tradev2onchain;
GRANT SELECT (intent_id,payload,exchange,account_id,environment,product)
 ON trade_v2.v2_trade_intents TO tradev2onchain;
GRANT SELECT (episode_id,status) ON trade_v2.v2_episodes TO tradev2onchain;
GRANT SELECT (order_id,episode_id,leg) ON trade_v2.v2_orders TO tradev2onchain;
GRANT SELECT (order_id,quantity) ON trade_v2.v2_fills TO tradev2onchain;
GRANT SELECT ON trade_v2.v2_onchain_mappings TO tradev2onchain;
GRANT SELECT,INSERT,DELETE ON trade_v2.v2_onchain_observations,trade_v2.v2_onchain_runs TO tradev2onchain;
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_onchain_provider_circuits TO tradev2onchain;
