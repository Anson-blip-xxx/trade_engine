-- Run in an explicit transaction after the shared-market migration.
-- No credentials, account routing, orders, evidence or current-state deletion.
DO $$
BEGIN
    IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='trade_v2_retention_owner') THEN
        CREATE ROLE trade_v2_retention_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS(SELECT FROM pg_roles WHERE rolname='trade_v2_retention_owner'
        AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'RETENTION_OWNER_PRIVILEGES_INVALID';
    END IF;
    IF EXISTS(SELECT FROM pg_auth_members WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname='trade_v2_retention_owner')
        OR member=(SELECT oid FROM pg_roles WHERE rolname='trade_v2_retention_owner')) THEN
        RAISE EXCEPTION 'RETENTION_OWNER_MEMBERSHIP_FORBIDDEN';
    END IF;
END $$;

CREATE TABLE v2_retention_policy (
    environment text PRIMARY KEY CHECK(environment='SANDBOX'),
    enabled boolean NOT NULL DEFAULT true,
    diagnostic_days integer NOT NULL DEFAULT 30 CHECK(diagnostic_days BETWEEN 30 AND 3650),
    demand_days integer NOT NULL DEFAULT 7 CHECK(demand_days BETWEEN 1 AND 3650),
    batch_rows integer NOT NULL DEFAULT 200 CHECK(batch_rows BETWEEN 1 AND 200),
    audit_days integer NOT NULL DEFAULT 365 CHECK(audit_days BETWEEN 365 AND 3650)
);
INSERT INTO v2_retention_policy(environment) VALUES ('SANDBOX');
CREATE TABLE v2_retention_runs (
    run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    result jsonb NOT NULL CHECK(jsonb_typeof(result)='object')
);

DO $migration$
DECLARE ns text := current_schema();
BEGIN
EXECUTE format($ddl$
CREATE FUNCTION %1$I.v2_retention_history_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
BEGIN
    IF TG_OP='DELETE' AND current_user='trade_v2_retention_owner'
       AND OLD.created_at < transaction_timestamp()-interval '30 days'
       AND EXISTS(SELECT FROM v2_business_state b WHERE b.state_id=OLD.state_id
          AND b.scope->>'environment'='SANDBOX' AND b.version>OLD.version
          AND (b.scope->>'namespace',OLD.reason) IN (
             ('host-disk-health-v1','HOST_DISK_ASSESSMENT'),
             ('testnet-trading-daemon-v1','TESTNET_TRADING_DAEMON_CYCLE'),
             ('trading-health-v1','TRADING_BUSINESS_HEALTH'),
             ('trading-pipeline-cycle-v1','TRADING_PIPELINE_CYCLE')))
       AND NOT EXISTS(SELECT FROM v2_s3_frames f WHERE (f.state_id,f.state_version)=(OLD.state_id,OLD.version))
    THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'immutable state history';
END;
$fn$;

CREATE FUNCTION %1$I.v2_run_retention(p_apply boolean DEFAULT false) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE policy v2_retention_policy%%ROWTYPE; ids uuid[]; demand_symbols text[];
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
    ms := floor(extract(epoch FROM transaction_timestamp()-make_interval(days=>policy.demand_days))*1000)::bigint;
    SELECT coalesce(array_agg(symbol),'{}'::text[]) INTO demand_symbols FROM (
      SELECT symbol FROM v2_market_demands WHERE environment='SANDBOX' AND expires_at_ms<ms
      ORDER BY expires_at_ms,symbol LIMIT policy.batch_rows FOR UPDATE SKIP LOCKED
    ) selected;
    IF p_apply THEN
        DELETE FROM v2_state_history WHERE event_id=ANY(ids);
        GET DIAGNOSTICS removed=ROW_COUNT;
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
        'event_ids_sha256',encode(sha256(convert_to(array_to_string(ids,','),'UTF8')),'hex'),
        'batch_full',cardinality(ids)=policy.batch_rows OR cardinality(demand_symbols)=policy.batch_rows);
    IF p_apply THEN INSERT INTO v2_retention_runs(result) VALUES(result); END IF;
    RETURN result;
END;
$fn$;
$ddl$,ns);
EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_run_retention(boolean) FROM PUBLIC',ns);
EXECUTE format('ALTER FUNCTION %I.v2_run_retention(boolean) OWNER TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT USAGE ON SCHEMA %I TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT SELECT ON %1$I.v2_business_state,%1$I.v2_state_history,%1$I.v2_s3_frames,%1$I.v2_retention_policy,%1$I.v2_market_demands,%1$I.v2_retention_runs TO trade_v2_retention_owner',ns);
-- Row locks require UPDATE privilege, but UPDATE still fails the immutable trigger.
EXECUTE format('GRANT UPDATE,DELETE ON %I.v2_state_history TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT UPDATE ON %I.v2_retention_policy TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT UPDATE,DELETE ON %I.v2_market_demands TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT INSERT,DELETE ON %I.v2_retention_runs TO trade_v2_retention_owner',ns);
EXECUTE format('GRANT USAGE ON SEQUENCE %I.v2_retention_runs_run_id_seq TO trade_v2_retention_owner',ns);
EXECUTE format('DROP TRIGGER v2_state_history_immutable ON %I.v2_state_history',ns);
EXECUTE format('CREATE TRIGGER v2_state_history_immutable BEFORE UPDATE OR DELETE ON %1$I.v2_state_history FOR EACH ROW EXECUTE FUNCTION %1$I.v2_retention_history_guard()',ns);
END;
$migration$;
