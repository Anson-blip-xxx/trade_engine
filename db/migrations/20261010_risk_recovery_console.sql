-- Additive Testnet-only manual probe. Never reset equity, peak, anchor or losses.
CREATE TABLE v2_risk_recovery_events (
    tenant_id uuid NOT NULL,
    registry_id uuid NOT NULL,
    request_id uuid NOT NULL,
    document jsonb NOT NULL,
    before_health jsonb NOT NULL,
    after_health jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_risk_recovery_events_immutable BEFORE UPDATE OR DELETE ON v2_risk_recovery_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

DO $migration$
DECLARE ns text := current_schema();
BEGIN
EXECUTE format($ddl$
CREATE FUNCTION %1$I.v2_web_risk_recovery_check(p_tenant uuid,p_registry uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE a record; r record; s record; p record; c record; t record;
    b jsonb := '[]'; vals jsonb; h jsonb; now_ms bigint; scope_base jsonb;
    dd numeric; factor numeric; retry_at bigint; proof jsonb;
BEGIN
    SELECT * INTO a FROM v2_tenant_accounts WHERE tenant_id=p_tenant AND registry_id=p_registry;
    IF NOT FOUND OR EXISTS(SELECT 1 FROM v2_account_retirements WHERE tenant_id=p_tenant AND registry_id=p_registry)
      THEN RAISE EXCEPTION 'RECOVERY_ACCOUNT_NOT_FOUND'; END IF;
    IF a.environment<>'SANDBOX' OR a.exchange<>'BINANCE' OR a.product<>'FUTURES'
      THEN RETURN jsonb_build_object('registry_id',p_registry,'eligible',false,'blockers',jsonb_build_array('TESTNET_ONLY')); END IF;
    now_ms := floor(extract(epoch FROM clock_timestamp())*1000)::bigint;
    scope_base := jsonb_build_object('exchange',a.exchange,'account_id',a.account_id,'environment',a.environment,'product',a.product);
    SELECT * INTO r FROM v2_execution_routes WHERE tenant_id=p_tenant AND environment=a.environment;
    IF r.target_registry IS DISTINCT FROM p_registry OR r.phase IS DISTINCT FROM 'ACTIVE'
       OR r.blockers IS DISTINCT FROM '[]'::jsonb OR r.binding_version IS DISTINCT FROM a.version
      THEN b := b || '"NOT_CURRENT_EXECUTOR"'::jsonb; END IF;
    IF EXISTS(SELECT 1 FROM v2_execution_route_events WHERE tenant_id=p_tenant AND environment=a.environment AND epoch>r.epoch)
      THEN b := b || '"ACCOUNT_SWITCH_PENDING"'::jsonb; END IF;
    SELECT * INTO p FROM v2_business_state WHERE scope=scope_base || '{"namespace":"runtime-policy-v1","key":"active"}'::jsonb AND NOT deleted;
    SELECT * INTO s FROM v2_business_state WHERE scope=scope_base || '{"namespace":"capital-snapshot-v1","key":"latest"}'::jsonb AND NOT deleted;
    vals := p.payload->'values'; h := s.payload->'capital_health';
    IF vals IS NULL OR h IS NULL THEN
      RETURN jsonb_build_object('registry_id',p_registry,'eligible',false,'blockers',b || '"CAPITAL_STATE_MISSING"'::jsonb);
    END IF;
    IF (vals->'capital.enabled') IS DISTINCT FROM 'true'::jsonb
       OR (vals->'capital.model_enabled') IS DISTINCT FROM 'true'::jsonb
       OR (vals->'portfolio_risk.enabled') IS DISTINCT FROM 'true'::jsonb
       OR (vals->'recovery.enabled') IS DISTINCT FROM 'true'::jsonb
       OR NOT ((vals->>'recovery.probe_factor')::numeric > 0 AND (vals->>'recovery.probe_factor')::numeric <= .20)
      THEN b := b || '"RECOVERY_POLICY_UNSUPPORTED"'::jsonb; END IF;
    IF h->'recovery'->>'mode' IS DISTINCT FROM 'HALTED' THEN b := b || '"NOT_HALTED"'::jsonb; END IF;
    -- Only exhaustion halts can use this narrow path. Other hard halts need a separate review.
    IF NOT coalesce((h->'recovery'->>'attempts')::int >= (vals->>'recovery.max_attempts')::int,false)
      THEN b := b || '"HARD_HALT_REVIEW_REQUIRED"'::jsonb; END IF;
    IF NOT coalesce((s.payload->>'started_at_ms')::bigint BETWEEN now_ms-60000 AND now_ms
        AND (s.payload->>'valid_until_ms')::bigint>now_ms,false)
      THEN b := b || '"CAPITAL_SNAPSHOT_STALE"'::jsonb; END IF;
    IF NOT EXISTS(SELECT 1 FROM v2_state_history ph WHERE ph.state_id=p.state_id AND ph.version=p.version
      AND ph.created_at<=to_timestamp((s.payload->>'started_at_ms')::numeric/1000))
      THEN b := b || '"POLICY_SNAPSHOT_NOT_OBSERVED"'::jsonb; END IF;
    IF NOT coalesce((h->>'base')::numeric>0 AND (h->>'peak')::numeric>0
       AND (h->>'base')::numeric<=(h->>'peak')::numeric,false)
      THEN b := b || '"CAPITAL_INVALID"'::jsonb;
    ELSE
      dd := ((h->>'peak')::numeric-(h->>'base')::numeric)/(h->>'peak')::numeric;
      IF NOT coalesce(dd < (vals->>'capital.halt_drawdown')::numeric,false)
        THEN b := b || '"HARD_DRAWDOWN_LIMIT"'::jsonb; END IF;
    END IF;
    retry_at := greatest(coalesce((h->'recovery'->>'paused_at_ms')::bigint,0),
                         coalesce((h->'recovery'->>'manual_recovery_at_ms')::bigint,0))
                + (vals->>'recovery.cooldown_ms')::bigint;
    IF retry_at IS NULL OR now_ms<retry_at THEN b := b || '"RECOVERY_COOLDOWN"'::jsonb; END IF;
    IF NOT coalesce((s.payload->'account'->>'totalInitialMargin')::numeric=0
       AND (s.payload->'account'->>'availableBalance')::numeric>0,false)
      THEN b := b || '"ACCOUNT_NOT_FLAT"'::jsonb; END IF;
    SELECT x.payload,y.created_at INTO c FROM v2_business_state x
      JOIN v2_state_history y ON y.state_id=x.state_id AND y.version=x.version
      WHERE x.scope=scope_base || '{"namespace":"account-coverage-v1","key":"latest"}'::jsonb AND NOT x.deleted;
    IF c.payload->>'status' IS DISTINCT FROM 'ACCOUNT_COVERAGE_CLEAR'
       OR c.payload->'blockers' IS DISTINCT FROM '[]'::jsonb
       OR c.created_at IS NULL OR c.created_at<clock_timestamp()-interval '120 seconds'
       OR c.created_at>clock_timestamp()
      THEN b := b || '"COVERAGE_NOT_FRESH_CLEAR"'::jsonb; END IF;
    SELECT * INTO t FROM v2_business_state WHERE scope=scope_base || '{"namespace":"trading-health-v1","key":"latest"}'::jsonb AND NOT deleted;
    IF t.payload->'active' IS DISTINCT FROM '["CAPITAL_DRAWDOWN_HALT"]'::jsonb
       OR NOT coalesce((t.payload->>'observed_at_ms')::bigint BETWEEN now_ms-120000 AND now_ms,false)
      THEN b := b || '"OTHER_HEALTH_BLOCKERS"'::jsonb; END IF;
    IF EXISTS(SELECT 1 FROM v2_trade_intents i LEFT JOIN v2_episodes e ON e.episode_id=i.intent_id
       WHERE (i.exchange,i.account_id,i.environment,i.product)=(a.exchange,a.account_id,a.environment,a.product)
         AND (e.status NOT IN ('SETTLED','ABORTED') OR (e.episode_id IS NULL AND i.status NOT IN ('REJECTED','EXPIRED','CANCELLED'))))
       OR EXISTS(SELECT 1 FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
       WHERE (i.exchange,i.account_id,i.environment,i.product)=(a.exchange,a.account_id,a.environment,a.product)
         AND o.status NOT IN ('FILLED','CANCELLED','REJECTED','RECONCILED'))
       OR EXISTS(SELECT 1 FROM v2_risk_reservations rr WHERE rr.scope::jsonb=scope_base AND rr.status='HELD')
      THEN b := b || '"UNSETTLED_BUSINESS"'::jsonb; END IF;
    factor := least((vals->>'recovery.probe_factor')::numeric,
      CASE WHEN dd >= (vals->>'capital.defensive_drawdown')::numeric THEN (vals->>'capital.defensive_factor')::numeric
      WHEN dd >= (vals->>'capital.reduce_drawdown')::numeric THEN (vals->>'capital.reduced_factor')::numeric ELSE 1 END);
    proof := jsonb_build_object('epoch',r.epoch,'binding_version',a.version,'policy_version',p.version,
      'anchor',h->'anchor','recovery',h->'recovery');
    RETURN jsonb_build_object('registry_id',p_registry,'environment',a.environment,'eligible',b='[]'::jsonb,
      'blockers',b,'health',h,'probe_factor',factor::text,'retry_at_ms',retry_at,'proof',proof,
      'expires_at_ms',now_ms+300000,'checked_at_ms',now_ms,'snapshot_at_ms',s.payload->'started_at_ms',
      'loss_fraction',vals->'recovery.loss_fraction','max_losses',vals->'recovery.max_losses',
      'min_settlements',vals->'recovery.min_settlements','active_drawdown',vals->'capital.reduce_drawdown',
      'halt_drawdown',vals->'capital.halt_drawdown');
END;
$fn$;
$ddl$,ns);
EXECUTE format($ddl$
CREATE FUNCTION %1$I.v2_web_risk_recovery_apply(p_tenant uuid,p_registry uuid,p_request uuid,p_document jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE a record; prior record; s record; check_result jsonb; h jsonb; new_h jsonb; new_payload jsonb;
    result jsonb; scope_key text; now_ms bigint;
BEGIN
    IF p_request IS NULL OR p_tenant IS NULL OR p_registry IS NULL
      OR jsonb_typeof(p_document) IS DISTINCT FROM 'object'
      OR NOT (p_document ?& ARRAY['actor','reason','proof','expires_at_ms'])
      OR p_document-ARRAY['actor','reason','proof','expires_at_ms']<>'{}'::jsonb
      OR jsonb_typeof(p_document->'actor') IS DISTINCT FROM 'string'
      OR jsonb_typeof(p_document->'reason') IS DISTINCT FROM 'string'
      OR length(trim(p_document->>'actor')) NOT BETWEEN 1 AND 120
      OR length(trim(p_document->>'reason')) NOT BETWEEN 5 AND 240
      OR jsonb_typeof(p_document->'proof') IS DISTINCT FROM 'object'
      OR coalesce(p_document->>'expires_at_ms','') !~ '^[0-9]{1,15}$'
      THEN RAISE EXCEPTION 'RECOVERY_REQUEST_INVALID'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('risk-recovery-request:'||p_tenant::text||':'||p_request::text,0));
    SELECT * INTO prior FROM v2_risk_recovery_events WHERE tenant_id=p_tenant AND request_id=p_request;
    IF FOUND THEN
      IF prior.registry_id<>p_registry OR prior.document<>p_document THEN RAISE EXCEPTION 'RECOVERY_REQUEST_CONFLICT'; END IF;
      RETURN prior.result;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('execution-route:'||p_tenant::text||':SANDBOX',0));
    SELECT * INTO a FROM v2_tenant_accounts WHERE tenant_id=p_tenant AND registry_id=p_registry FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'RECOVERY_ACCOUNT_NOT_FOUND'; END IF;
    IF a.environment<>'SANDBOX' OR a.account_id !~ '^[A-Za-z0-9_.:-]{1,160}$'
      THEN RAISE EXCEPTION 'RECOVERY_NOT_ELIGIBLE'; END IF;
    PERFORM 1 FROM v2_execution_routes WHERE tenant_id=p_tenant AND environment=a.environment FOR SHARE;
    scope_key := '{"account_id":'||to_jsonb(a.account_id)::text||',"environment":"SANDBOX","exchange":"BINANCE","product":"FUTURES"}';
    PERFORM pg_advisory_xact_lock(hashtextextended('testnet-protocol-account:'||scope_key,0));
    SELECT * INTO s FROM v2_business_state WHERE scope=scope_key::jsonb || '{"namespace":"capital-snapshot-v1","key":"latest"}'::jsonb FOR UPDATE;
    now_ms := floor(extract(epoch FROM clock_timestamp())*1000)::bigint;
    IF (p_document->>'expires_at_ms')::bigint<=now_ms OR (p_document->>'expires_at_ms')::bigint>now_ms+300000
      THEN RAISE EXCEPTION 'RECOVERY_PREVIEW_EXPIRED'; END IF;
    check_result := v2_web_risk_recovery_check(p_tenant,p_registry);
    IF check_result->'proof' IS DISTINCT FROM p_document->'proof' THEN RAISE EXCEPTION 'RECOVERY_STATE_CHANGED'; END IF;
    IF check_result->'eligible' IS DISTINCT FROM 'true'::jsonb THEN RAISE EXCEPTION 'RECOVERY_NOT_ELIGIBLE'; END IF;
    h := s.payload->'capital_health';
    new_h := h || jsonb_build_object('factor',check_result->'probe_factor','recovery',
      (h->'recovery') || jsonb_build_object('mode','PROBE','probe_started_at_ms',now_ms,'probe_base',h->'base',
         'manual_recovery_at_ms',now_ms,'manual_request_id',p_request));
    new_payload := jsonb_set(s.payload,'{capital_health}',new_h);
    UPDATE v2_business_state SET payload=new_payload,version=s.version+1 WHERE state_id=s.state_id;
    INSERT INTO v2_state_history(event_id,state_id,request_key,expected_version,version,payload,deleted,reason)
      VALUES(gen_random_uuid(),s.state_id,'web-risk-recovery:'||p_request::text,s.version,s.version+1,new_payload,false,'WEB_RISK_RECOVERY_PROBE');
    result := jsonb_build_object('registry_id',p_registry,'status','PROBE_AUTHORIZED','request_id',p_request,
      'at_ms',now_ms,'probe_factor',check_result->'probe_factor','orders_created',0);
    INSERT INTO v2_risk_recovery_events(tenant_id,registry_id,request_id,document,before_health,after_health,result)
      VALUES(p_tenant,p_registry,p_request,p_document,h,new_h,result);
    RETURN result;
END;
$fn$;
$ddl$,ns);
EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_web_risk_recovery_check(uuid,uuid) FROM PUBLIC',ns);
EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_web_risk_recovery_apply(uuid,uuid,uuid,jsonb) FROM PUBLIC',ns);
END;
$migration$;
