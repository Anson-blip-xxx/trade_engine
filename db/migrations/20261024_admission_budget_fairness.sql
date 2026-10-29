-- Close the Web verification bypass of host-level noncritical lane reserves.
-- Apply after 20260930_web_verification_quota.sql. Reapplication preserves the
-- existing function OID, owner and ACL: no Web table privileges or new grants.
-- During a mixed-binary rollout keep both reserves at zero. Enable them only
-- after all noncritical writers are upgraded and a fresh DB minute has begun;
-- old, unclassified host usage is conservatively seeded as market usage.
DO $migration$
DECLARE ns text := current_schema();
BEGIN
 IF to_regprocedure(format('%I.v2_web_testnet_verify_permit()',ns)) IS NULL THEN
   RAISE EXCEPTION 'WEB_VERIFICATION_FUNCTION_REQUIRED';
 END IF;
 EXECUTE format($ddl$
 CREATE OR REPLACE FUNCTION %1$I.v2_web_testnet_verify_permit() RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $fn$
 DECLARE
   cfg jsonb; stored_scope jsonb; disabled boolean;
   admission_cfg jsonb; admission_disabled boolean;
   total_limit integer := 1800; scan_limit integer := 900;
   admission_reserve integer := 0; market_reserve integer := 0;
   window_now bigint; previous_window bigint; used integer; scan_used integer;
   lane_window bigint; lane_used integer;
   blocked timestamptz; now_stamp timestamptz;
 BEGIN
   BEGIN
     SELECT payload,deleted,scope INTO STRICT cfg,disabled,stored_scope
       FROM %1$I.v2_business_state
       WHERE state_id='f2e6fd90-7a76-53ef-b65f-ef1f61570c48'::uuid;
     IF stored_scope IS DISTINCT FROM '{"exchange":"BINANCE","account_id":"market-data","environment":"SANDBOX","product":"FUTURES","namespace":"shared-market-policy","key":"current"}'::jsonb THEN
       RAISE EXCEPTION 'MARKET_POLICY_IDENTITY_CONFLICT';
     END IF;
   EXCEPTION WHEN no_data_found THEN cfg := '{}'::jsonb; disabled := false;
   END;
   IF disabled IS DISTINCT FROM false THEN RAISE EXCEPTION 'MARKET_POLICY_DISABLED'; END IF;
   IF jsonb_typeof(cfg) IS DISTINCT FROM 'object' THEN
     RAISE EXCEPTION 'MARKET_POLICY_BOUNDS';
   END IF;
   IF cfg ? 'shared_weight_limit' THEN
     IF jsonb_typeof(cfg->'shared_weight_limit') IS DISTINCT FROM 'number'
        OR NOT (cfg->>'shared_weight_limit' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'MARKET_POLICY_BOUNDS';
     END IF;
     total_limit := (cfg->>'shared_weight_limit')::integer;
   END IF;
   IF cfg ? 'market_weight_limit' THEN
     IF jsonb_typeof(cfg->'market_weight_limit') IS DISTINCT FROM 'number'
        OR NOT (cfg->>'market_weight_limit' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'MARKET_POLICY_BOUNDS';
     END IF;
     scan_limit := (cfg->>'market_weight_limit')::integer;
   END IF;
   IF total_limit NOT BETWEEN 100 AND 2400 OR scan_limit NOT BETWEEN 50 AND 1800
      OR scan_limit >= total_limit THEN RAISE EXCEPTION 'MARKET_POLICY_BOUNDS'; END IF;

   BEGIN
     SELECT payload,deleted,scope INTO STRICT admission_cfg,admission_disabled,stored_scope
       FROM %1$I.v2_business_state
       WHERE state_id='09f12c30-e371-596f-8f1a-1715b7c7e31b'::uuid;
     IF stored_scope IS DISTINCT FROM '{"exchange":"BINANCE","account_id":"market-data","environment":"SANDBOX","product":"FUTURES","namespace":"admission-budget-policy","key":"current"}'::jsonb THEN
       RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_IDENTITY_CONFLICT';
     END IF;
   EXCEPTION WHEN no_data_found THEN admission_cfg := '{}'::jsonb; admission_disabled := false;
   END;
   IF admission_disabled IS DISTINCT FROM false THEN
     RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_DISABLED';
   END IF;
   IF jsonb_typeof(admission_cfg) IS DISTINCT FROM 'object' THEN
     RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_BOUNDS';
   END IF;
   IF EXISTS (SELECT 1 FROM jsonb_object_keys(admission_cfg) AS keys(name)
              WHERE name NOT IN ('admission_reserved_weight','market_reserved_weight')) THEN
     RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_BOUNDS';
   END IF;
   IF admission_cfg ? 'admission_reserved_weight' THEN
     IF jsonb_typeof(admission_cfg->'admission_reserved_weight') IS DISTINCT FROM 'number'
        OR NOT (admission_cfg->>'admission_reserved_weight' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_BOUNDS';
     END IF;
     admission_reserve := (admission_cfg->>'admission_reserved_weight')::integer;
   END IF;
   IF admission_cfg ? 'market_reserved_weight' THEN
     IF jsonb_typeof(admission_cfg->'market_reserved_weight') IS DISTINCT FROM 'number'
        OR NOT (admission_cfg->>'market_reserved_weight' ~ '^[0-9]{1,4}$') THEN
       RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_BOUNDS';
     END IF;
     market_reserve := (admission_cfg->>'market_reserved_weight')::integer;
   END IF;
   IF admission_reserve NOT BETWEEN 0 AND 1750 OR market_reserve NOT BETWEEN 0 AND 1800 THEN
     RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_BOUNDS';
   END IF;
   IF admission_reserve > scan_limit-50 OR admission_reserve+market_reserve > scan_limit THEN
     RAISE EXCEPTION 'ADMISSION_BUDGET_POLICY_RESERVE_INVALID';
   END IF;

   -- Same lock ordering as HostBudget: host row, then market-lane row.
   INSERT INTO %1$I.v2_egress_budgets(host) VALUES('demo-fapi.binance.com') ON CONFLICT DO NOTHING;
   SELECT window_id,used_weight,market_weight,blocked_until
     INTO previous_window,used,scan_used,blocked FROM %1$I.v2_egress_budgets
     WHERE host='demo-fapi.binance.com' FOR UPDATE;
   -- Read database time AFTER waiting for the lock, never caller-provided time.
   now_stamp := clock_timestamp();
   window_now := floor(extract(epoch FROM now_stamp)/60)::bigint;
   IF previous_window IS NULL OR used IS NULL OR scan_used IS NULL OR blocked IS NULL
      OR previous_window < 0 OR used < 0 OR scan_used < 0 OR scan_used > used THEN
     RAISE EXCEPTION 'EGRESS_LEDGER_INVALID';
   END IF;
   IF window_now < previous_window OR blocked > now_stamp THEN RETURN false; END IF;
   IF window_now <> previous_window THEN used := 0; scan_used := 0; END IF;
   -- Fixed GET /fapi/v1/positionSide/dual cost; management reserve is untouched.
   IF used+30 > total_limit OR scan_used+30 > scan_limit THEN RETURN false; END IF;
   INSERT INTO %1$I.v2_public_market_budgets(scope,weight_limit,window_id,used_weight)
     VALUES('v2-host-lane:market:demo-fapi.binance.com',2400,window_now,scan_used)
     ON CONFLICT DO NOTHING;
   SELECT window_id,used_weight INTO lane_window,lane_used
     FROM %1$I.v2_public_market_budgets
     WHERE scope='v2-host-lane:market:demo-fapi.binance.com' FOR UPDATE;
   IF lane_window IS NULL OR lane_used IS NULL OR lane_window < 0 OR lane_used < 0 THEN
     RAISE EXCEPTION 'EGRESS_LANE_LEDGER_INVALID';
   END IF;
   IF lane_window > window_now THEN RETURN false; END IF;
   IF lane_window <> window_now THEN lane_used := 0; END IF;
   IF lane_used > scan_used THEN RAISE EXCEPTION 'EGRESS_LANE_LEDGER_INVALID'; END IF;
   -- Verification belongs to admission, not market. Do not increment the
   -- market-lane counter or gain a separate allowance for account verification.
   IF scan_used-lane_used+30 > scan_limit-market_reserve THEN RETURN false; END IF;
   UPDATE %1$I.v2_egress_budgets
     SET window_id=window_now,used_weight=used+30,market_weight=scan_used+30
     WHERE host='demo-fapi.binance.com';
   RETURN true;
 END;
 $fn$;
 $ddl$,ns);
END;
$migration$;
