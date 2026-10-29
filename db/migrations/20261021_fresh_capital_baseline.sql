-- Trusted controller receives a boolean, never registry/cash-table access.
-- Apply as postgres with search_path set to the intended deployment schema.
-- Installation binds and quotes the schema once; the function has no dynamic SQL.
DO $migration$
DECLARE ns text := current_schema();
BEGIN
 IF ns IS NULL OR ns IN ('public','pg_catalog','information_schema') THEN
   RAISE EXCEPTION 'EXPLICIT_TRADING_SCHEMA_REQUIRED';
 END IF;
 EXECUTE format($ddl$
 CREATE FUNCTION %1$I.v2_fresh_capital_scope_clear(
   p_exchange text,p_account text,p_environment text,p_product text
 ) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,%1$I SET lock_timeout='1s' AS $fn$
 BEGIN
   IF p_exchange IS DISTINCT FROM 'BINANCE'
      OR p_environment IS DISTINCT FROM 'SANDBOX'
      OR p_product IS DISTINCT FROM 'FUTURES'
      OR p_account IS NULL OR p_account !~ '^[A-Za-z0-9_.:/-]{1,128}$' THEN
     RAISE EXCEPTION 'FRESH_CAPITAL_SANDBOX_SCOPE_REQUIRED';
   END IF;
   -- Retained until the caller commits its policy CAS. No table mutation.
   LOCK TABLE %1$I.v2_trade_intents,%1$I.v2_settlements,
     %1$I.v2_capital_journals,%1$I.v2_capital_entries,
     %1$I.v2_exchange_income,%1$I.v2_income_imports IN SHARE MODE;
   RETURN NOT (
     EXISTS(SELECT 1 FROM %1$I.v2_trade_intents i
       WHERE (i.exchange,i.account_id,i.environment,i.product)=
         (p_exchange,p_account,p_environment,p_product))
     OR EXISTS(SELECT 1 FROM %1$I.v2_settlements s
       JOIN %1$I.v2_trade_intents i ON i.intent_id=s.episode_id
       WHERE (i.exchange,i.account_id,i.environment,i.product)=
         (p_exchange,p_account,p_environment,p_product))
     OR EXISTS(SELECT 1 FROM %1$I.v2_capital_journals j
       JOIN %1$I.v2_tenant_accounts a USING(tenant_id,registry_id)
       WHERE (a.exchange,a.account_id,a.environment,a.product)=
         (p_exchange,p_account,p_environment,p_product))
     OR EXISTS(SELECT 1 FROM %1$I.v2_capital_entries e
       JOIN %1$I.v2_tenant_accounts a USING(tenant_id,registry_id)
       WHERE (a.exchange,a.account_id,a.environment,a.product)=
         (p_exchange,p_account,p_environment,p_product))
     OR EXISTS(SELECT 1 FROM %1$I.v2_exchange_income i
       WHERE (i.exchange,i.account_id,i.environment,i.product)=
         (p_exchange,p_account,p_environment,p_product))
     OR EXISTS(SELECT 1 FROM %1$I.v2_income_imports
       WHERE scope=jsonb_build_object('exchange',p_exchange,'account_id',p_account,
         'environment',p_environment,'product',p_product))
   );
 END
 $fn$;
 $ddl$,ns);
 EXECUTE format('ALTER FUNCTION %I.v2_fresh_capital_scope_clear(text,text,text,text) OWNER TO postgres',ns);
 EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_fresh_capital_scope_clear(text,text,text,text) FROM PUBLIC',ns);
 EXECUTE format('GRANT EXECUTE ON FUNCTION %I.v2_fresh_capital_scope_clear(text,text,text,text) TO ubuntu',ns);
END
$migration$;
