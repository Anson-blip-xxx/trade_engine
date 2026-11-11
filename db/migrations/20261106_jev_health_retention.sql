-- Extend the existing hourly retention job, after 20261004_diagnostic_retention.
-- No new role, grants, task, trading-table access or deletion at migration time.
-- This one GLOBAL diagnostic identity is not a blanket GLOBAL/JEV allowlist.
-- Keep the existing function owner, ACL, advisory lock and all previous rules.
DO $migration$
DECLARE
    ns text := current_schema();
    health_id uuid := 'dffc450d-c51b-5def-a0d8-f1135a612573';
    health_scope jsonb := '{"account_id":"shared-jev-primary","environment":"GLOBAL","exchange":"JEV","key":"latest","namespace":"jev-advisory-health-v1","product":"ADVISORY"}';
    runner regprocedure;
    guard regprocedure;
BEGIN
    runner := to_regprocedure(format('%I.v2_run_retention(boolean)',ns));
    guard := to_regprocedure(format('%I.v2_retention_history_guard()',ns));
    IF runner IS NULL OR guard IS NULL OR NOT EXISTS(
        SELECT FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
        WHERE p.oid=runner AND r.rolname='trade_v2_retention_owner' AND p.prosecdef
          AND NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls
    ) OR NOT EXISTS(
        SELECT FROM pg_trigger t
        WHERE t.tgrelid=to_regclass(format('%I.v2_state_history',ns))
          AND t.tgname='v2_state_history_immutable' AND t.tgfoid=guard AND t.tgenabled='O'
    ) THEN
        RAISE EXCEPTION 'EXISTING_CONTROLLED_RETENTION_REQUIRED';
    END IF;

    EXECUTE format($ddl$
CREATE OR REPLACE FUNCTION %1$I.v2_retention_history_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
BEGIN
    IF TG_OP='DELETE' AND current_user='trade_v2_retention_owner'
       AND OLD.created_at < transaction_timestamp()-interval '30 days'
       AND EXISTS(SELECT FROM v2_business_state b WHERE b.state_id=OLD.state_id
          AND b.version>OLD.version AND (
            (b.scope->>'environment'='SANDBOX'
             AND (b.scope->>'namespace',OLD.reason) IN (
               ('host-disk-health-v1','HOST_DISK_ASSESSMENT'),
               ('testnet-trading-daemon-v1','TESTNET_TRADING_DAEMON_CYCLE'),
               ('trading-health-v1','TRADING_BUSINESS_HEALTH'),
               ('trading-pipeline-cycle-v1','TRADING_PIPELINE_CYCLE')))
            OR (b.state_id=%2$L::uuid AND b.scope=%3$L::jsonb AND NOT b.deleted
                AND OLD.reason='JEV durable advisory coordination' AND NOT OLD.deleted
                AND OLD.payload->>'schema'='jev-completed-model-health-v1'
                AND OLD.payload->'execution_authorized'='false'::jsonb)))
       AND NOT EXISTS(SELECT FROM v2_s3_frames f WHERE (f.state_id,f.state_version)=(OLD.state_id,OLD.version))
    THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'immutable state history';
END;
$fn$;

CREATE OR REPLACE FUNCTION %1$I.v2_run_retention(p_apply boolean DEFAULT false) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE policy v2_retention_policy%%ROWTYPE; ids uuid[]; demand_symbols text[];
    health_ids uuid[]; health_removed integer:=0;
    removed integer:=0; demands_removed integer:=0; audit_removed integer:=0;
    result jsonb; cutoff timestamptz; ms bigint;
BEGIN
    IF p_apply IS NULL THEN RAISE EXCEPTION 'EXPLICIT_RETENTION_MODE_REQUIRED'; END IF;
    IF NOT pg_try_advisory_xact_lock(hashtextextended('v2-diagnostic-retention:' || %1$L,0)) THEN
        RETURN jsonb_build_object('status','BUSY');
    END IF;
    SELECT * INTO STRICT policy FROM v2_retention_policy WHERE environment='SANDBOX' FOR SHARE;
    IF NOT policy.enabled THEN RETURN jsonb_build_object('status','DISABLED'); END IF;
    cutoff := transaction_timestamp()-make_interval(days=>policy.diagnostic_days);
    SELECT coalesce(array_agg(event_id),'{}'::uuid[]) INTO ids FROM (
      SELECT h.event_id FROM v2_state_history h JOIN v2_business_state b ON b.state_id=h.state_id
      WHERE h.created_at<cutoff AND h.version<b.version AND b.scope->>'environment'='SANDBOX'
        AND h.reason IN ('HOST_DISK_ASSESSMENT','TESTNET_TRADING_DAEMON_CYCLE','TRADING_BUSINESS_HEALTH','TRADING_PIPELINE_CYCLE')
        AND (b.scope->>'namespace',h.reason) IN (
           ('host-disk-health-v1','HOST_DISK_ASSESSMENT'),
           ('testnet-trading-daemon-v1','TESTNET_TRADING_DAEMON_CYCLE'),
           ('trading-health-v1','TRADING_BUSINESS_HEALTH'),
           ('trading-pipeline-cycle-v1','TRADING_PIPELINE_CYCLE'))
        AND NOT EXISTS(SELECT FROM v2_s3_frames f WHERE (f.state_id,f.state_version)=(h.state_id,h.version))
      ORDER BY h.created_at,h.event_id LIMIT policy.batch_rows FOR UPDATE OF h SKIP LOCKED
    ) selected;
    -- The existing (state_id,version) unique index bounds this to ONE key;
    -- sorting by version avoids a global recent-history/JSON/TOAST scan.
    SELECT coalesce(array_agg(event_id),'{}'::uuid[]) INTO health_ids FROM (
      SELECT h.event_id FROM v2_state_history h JOIN v2_business_state b ON b.state_id=h.state_id
      WHERE h.state_id=%2$L::uuid AND b.scope=%3$L::jsonb AND NOT b.deleted
        AND h.created_at<cutoff AND h.version<b.version AND NOT h.deleted
        AND h.reason='JEV durable advisory coordination'
        AND h.payload->>'schema'='jev-completed-model-health-v1'
        AND h.payload->'execution_authorized'='false'::jsonb
        AND NOT EXISTS(SELECT FROM v2_s3_frames f WHERE (f.state_id,f.state_version)=(h.state_id,h.version))
      ORDER BY h.version LIMIT policy.batch_rows FOR UPDATE OF h SKIP LOCKED
    ) selected;
    ms := floor(extract(epoch FROM transaction_timestamp()-make_interval(days=>policy.demand_days))*1000)::bigint;
    SELECT coalesce(array_agg(symbol),'{}'::text[]) INTO demand_symbols FROM (
      SELECT symbol FROM v2_market_demands WHERE environment='SANDBOX' AND expires_at_ms<ms
      ORDER BY expires_at_ms,symbol LIMIT policy.batch_rows FOR UPDATE SKIP LOCKED
    ) selected;
    IF p_apply THEN
        DELETE FROM v2_state_history WHERE event_id=ANY(ids);
        GET DIAGNOSTICS removed=ROW_COUNT;
        DELETE FROM v2_state_history WHERE event_id=ANY(health_ids);
        GET DIAGNOSTICS health_removed=ROW_COUNT;
        DELETE FROM v2_market_demands WHERE environment='SANDBOX' AND symbol=ANY(demand_symbols) AND expires_at_ms<ms;
        GET DIAGNOSTICS demands_removed=ROW_COUNT;
        DELETE FROM v2_retention_runs WHERE run_id IN (
            SELECT run_id FROM v2_retention_runs
            WHERE completed_at<transaction_timestamp()-make_interval(days=>policy.audit_days)
            ORDER BY completed_at LIMIT policy.batch_rows);
        GET DIAGNOSTICS audit_removed=ROW_COUNT;
    END IF;
    result := jsonb_build_object('status',CASE WHEN p_apply THEN 'APPLIED' ELSE 'DRY_RUN' END,
        'environment','SANDBOX','diagnostic_cutoff',cutoff,'policy',to_jsonb(policy),
        'diagnostic_candidates',cardinality(ids),'expired_demand_candidates',cardinality(demand_symbols),
        'diagnostic_removed',removed,'expired_demands_removed',demands_removed,'audit_removed',audit_removed,
        'shared_jev_health_scope',%3$L::jsonb,'shared_jev_health_cutoff',cutoff,
        'shared_jev_health_candidates',cardinality(health_ids),'shared_jev_health_removed',health_removed,
        'shared_jev_health_event_ids_sha256',encode(sha256(convert_to(array_to_string(health_ids,','),'UTF8')),'hex'),
        'event_ids_sha256',encode(sha256(convert_to(array_to_string(ids,','),'UTF8')),'hex'),
        'batch_full',cardinality(ids)=policy.batch_rows OR cardinality(demand_symbols)=policy.batch_rows
            OR cardinality(health_ids)=policy.batch_rows);
    IF p_apply THEN INSERT INTO v2_retention_runs(result) VALUES(result); END IF;
    RETURN result;
END;
$fn$;
$ddl$,ns,health_id,health_scope);
END;
$migration$;
