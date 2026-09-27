-- The Web gets EXECUTE only. No caller-supplied host, lane, limit, clock or cost.
-- Schema names are bound at installation, never resolved through a caller path.
DO $migration$
DECLARE ns text := current_schema();
BEGIN
 EXECUTE format($ddl$
 CREATE FUNCTION %1$I.v2_web_testnet_verify_permit() RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $fn$
 DECLARE
   cfg jsonb; stored_scope jsonb; disabled boolean;
   total_limit integer := 1800; scan_limit integer := 900;
   window_now bigint; previous_window bigint; used integer; scan_used integer;
   blocked timestamptz;
 BEGIN
   BEGIN
     -- StateKey.identity for the scope below: use the PK, not a JSON table scan.
     SELECT payload,deleted,scope INTO STRICT cfg,disabled,stored_scope FROM %1$I.v2_business_state
     WHERE state_id='f2e6fd90-7a76-53ef-b65f-ef1f61570c48'::uuid;
     IF stored_scope IS DISTINCT FROM '{"exchange":"BINANCE","account_id":"market-data","environment":"SANDBOX","product":"FUTURES","namespace":"shared-market-policy","key":"current"}'::jsonb THEN
       RAISE EXCEPTION 'MARKET_POLICY_IDENTITY_CONFLICT';
     END IF;
   EXCEPTION WHEN no_data_found THEN cfg := '{}'::jsonb; disabled := false;
   END;
   IF disabled THEN RAISE EXCEPTION 'MARKET_POLICY_DISABLED'; END IF;
   IF cfg ? 'shared_weight_limit' THEN
     IF jsonb_typeof(cfg->'shared_weight_limit') <> 'number'
        OR NOT (cfg->>'shared_weight_limit' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'MARKET_POLICY_BOUNDS';
     END IF;
     total_limit := (cfg->>'shared_weight_limit')::integer;
   END IF;
   IF cfg ? 'market_weight_limit' THEN
     IF jsonb_typeof(cfg->'market_weight_limit') <> 'number'
        OR NOT (cfg->>'market_weight_limit' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'MARKET_POLICY_BOUNDS';
     END IF;
     scan_limit := (cfg->>'market_weight_limit')::integer;
   END IF;
   IF total_limit NOT BETWEEN 100 AND 2400 OR scan_limit NOT BETWEEN 50 AND 1800
      OR scan_limit >= total_limit THEN RAISE EXCEPTION 'MARKET_POLICY_BOUNDS'; END IF;
   INSERT INTO %1$I.v2_egress_budgets(host) VALUES('demo-fapi.binance.com') ON CONFLICT DO NOTHING;
   SELECT window_id,used_weight,market_weight,blocked_until
     INTO previous_window,used,scan_used,blocked FROM %1$I.v2_egress_budgets
     WHERE host='demo-fapi.binance.com' FOR UPDATE;
   -- Only database time may roll a minute window; Web has no reset operation.
   window_now := floor(extract(epoch FROM clock_timestamp())/60)::bigint;
   IF window_now < previous_window OR blocked > clock_timestamp() THEN RETURN false; END IF;
   IF window_now <> previous_window THEN used := 0; scan_used := 0; END IF;
   -- Fixed protocol cost of GET /fapi/v1/positionSide/dual. Charge noncritical
   -- scan allowance so Web verification cannot consume management's reserve.
   IF used + 30 > total_limit OR scan_used + 30 > scan_limit THEN RETURN false; END IF;
   UPDATE %1$I.v2_egress_budgets SET window_id=window_now,used_weight=used+30,market_weight=scan_used+30
     WHERE host='demo-fapi.binance.com';
   RETURN true;
 END
 $fn$;
 $ddl$,ns);

 EXECUTE format($ddl$
 CREATE FUNCTION %1$I.v2_web_testnet_quota_cooldown(seconds integer) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $fn$
 BEGIN
   IF seconds IS NULL OR seconds NOT BETWEEN 1 AND 259200 THEN
     RAISE EXCEPTION 'EGRESS_COOLDOWN_INVALID';
   END IF;
   INSERT INTO %1$I.v2_egress_budgets(host) VALUES('demo-fapi.binance.com') ON CONFLICT DO NOTHING;
   UPDATE %1$I.v2_egress_budgets
     SET blocked_until=greatest(blocked_until,clock_timestamp()+make_interval(secs=>seconds))
     WHERE host='demo-fapi.binance.com';
 END
 $fn$;
 $ddl$,ns);
 EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_web_testnet_verify_permit() FROM PUBLIC',ns);
 EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_web_testnet_quota_cooldown(integer) FROM PUBLIC',ns);
END
$migration$;
