-- Additive, opt-in. No policies are installed or activated by this migration.
-- Apply after tenant_registry/account_retirements and the core state schema.
CREATE TABLE v2_policy_console_events (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    request_id UUID NOT NULL,
    expected_version BIGINT NOT NULL,
    request JSONB NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_policy_console_events_immutable BEFORE UPDATE OR DELETE ON v2_policy_console_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

DO $migration$
DECLARE ns text := current_schema();
BEGIN
EXECUTE format($ddl$
CREATE FUNCTION %1$I.v2_web_policy_patch(p_tenant uuid,p_registry uuid,p_expected bigint,p_request uuid,p_document jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,%1$I,pg_temp AS $fn$
DECLARE a record; s record; prior record; changes jsonb; vals jsonb; result jsonb;
    k text; v jsonb; n numeric; lo numeric; hi numeric; scope_key text; new_payload jsonb;
BEGIN
    IF p_tenant IS NULL OR p_registry IS NULL OR p_request IS NULL OR p_expected IS NULL OR p_expected<1
       OR jsonb_typeof(p_document) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_document->'changes') IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_document->'reason') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_document->'actor') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_document->'preview_digest') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_document->'preview_expires_at_ms') IS DISTINCT FROM 'number'
       OR (p_document->>'preview_expires_at_ms') !~ '^[0-9]{1,15}$'
       OR length(p_document->>'reason') NOT BETWEEN 1 AND 240
       OR length(p_document->>'actor') NOT BETWEEN 1 AND 120
       OR (p_document->>'preview_digest') !~ '^[0-9a-f]{64}$'
       OR NOT (p_document ?& ARRAY['changes','reason','actor','preview_digest','restore_version','preview_expires_at_ms'])
       OR p_document - ARRAY['changes','reason','actor','preview_digest','restore_version','preview_expires_at_ms'] <> '{}'::jsonb
    THEN RAISE EXCEPTION 'POLICY_REQUEST_INVALID'; END IF;
    SELECT exchange,account_id,environment,product INTO a FROM v2_tenant_accounts
      WHERE tenant_id=p_tenant AND registry_id=p_registry FOR SHARE;
    IF NOT FOUND OR EXISTS(SELECT 1 FROM v2_account_retirements WHERE tenant_id=p_tenant AND registry_id=p_registry)
      THEN RAISE EXCEPTION 'POLICY_ACCOUNT_NOT_FOUND'; END IF;
    IF a.environment<>'SANDBOX' OR a.exchange<>'BINANCE' OR a.product<>'FUTURES'
      THEN RAISE EXCEPTION 'POLICY_SANDBOX_ONLY'; END IF;
    -- AccountScope canonical encoding for the account identities supported here.
    -- Reject non-ASCII identities rather than acquire a differently encoded lock.
    IF a.account_id !~ '^[A-Za-z0-9_.:-]{1,160}$' THEN RAISE EXCEPTION 'POLICY_ACCOUNT_ID_UNSUPPORTED'; END IF;
    scope_key := '{"account_id":' || to_jsonb(a.account_id)::text || ',"environment":"SANDBOX","exchange":"BINANCE","product":"FUTURES"}';
    PERFORM pg_advisory_xact_lock(hashtextextended('testnet-protocol-account:' || scope_key,0));
    SELECT * INTO prior FROM v2_policy_console_events WHERE tenant_id=p_tenant AND request_id=p_request;
    IF FOUND THEN
      IF prior.registry_id<>p_registry OR prior.expected_version<>p_expected OR prior.request<>p_document
        THEN RAISE EXCEPTION 'POLICY_REQUEST_CONFLICT'; END IF;
      RETURN prior.result;
    END IF;
    IF (p_document->>'preview_expires_at_ms')::bigint <= (extract(epoch FROM clock_timestamp())*1000)::bigint
       OR (p_document->>'preview_expires_at_ms')::bigint > (extract(epoch FROM clock_timestamp())*1000)::bigint + 300000
       THEN RAISE EXCEPTION 'POLICY_PREVIEW_EXPIRED'; END IF;
    SELECT * INTO s FROM v2_business_state WHERE scope=jsonb_build_object(
      'exchange',a.exchange,'account_id',a.account_id,'environment',a.environment,'product',a.product,
      'namespace','runtime-policy-v1','key','active') FOR UPDATE;
    IF NOT FOUND OR s.deleted OR s.version<>p_expected THEN RAISE EXCEPTION 'POLICY_VERSION_CONFLICT'; END IF;
    changes := p_document->'changes';
    IF changes='{}'::jsonb THEN RAISE EXCEPTION 'POLICY_NO_CHANGE'; END IF;
    FOR k,v IN SELECT * FROM jsonb_each(changes) LOOP
      IF k NOT IN ('capital.pool_fraction','sizing.pool_fraction','capital.margin_buffer',
        'sizing.max_margin_fraction','sizing.risk_fraction','portfolio_risk.max_fraction',
        'sizing.min_allocation','sizing.max_allocation','sizing.cost_buffer_fraction')
        THEN RAISE EXCEPTION 'POLICY_FIELD_NOT_EDITABLE'; END IF;
      IF jsonb_typeof(v)<>'string' OR length(v #>> '{}')>80 OR (v #>> '{}') !~ '^[0-9]+([.][0-9]+)?$'
        THEN RAISE EXCEPTION 'POLICY_VALUE_INVALID'; END IF;
      n := (v #>> '{}')::numeric;
      lo := CASE k WHEN 'capital.margin_buffer' THEN 1 WHEN 'sizing.risk_fraction' THEN .00001
        WHEN 'sizing.min_allocation' THEN 0 WHEN 'sizing.cost_buffer_fraction' THEN 0
        WHEN 'capital.pool_fraction' THEN .01 WHEN 'sizing.pool_fraction' THEN .01 ELSE .001 END;
      hi := CASE k WHEN 'capital.margin_buffer' THEN 2 WHEN 'sizing.cost_buffer_fraction' THEN .1
        WHEN 'capital.pool_fraction' THEN .70 WHEN 'sizing.pool_fraction' THEN .70 ELSE 1 END;
      IF n<lo OR n>hi THEN RAISE EXCEPTION 'POLICY_VALUE_INVALID'; END IF;
    END LOOP;
    vals := (s.payload->'values') || changes;
    IF (vals->'capital.enabled') IS DISTINCT FROM 'true'::jsonb
       OR (vals->'portfolio_risk.enabled') IS DISTINCT FROM 'true'::jsonb
       OR NOT (vals ?& ARRAY['capital.pool_fraction','sizing.pool_fraction','sizing.max_margin_fraction',
          'sizing.risk_fraction','portfolio_risk.max_fraction','sizing.min_allocation','sizing.max_allocation'])
       OR (vals->>'capital.pool_fraction')::numeric > .70
       OR (vals->>'capital.pool_fraction')::numeric <> (vals->>'sizing.pool_fraction')::numeric
       OR (vals->>'sizing.max_margin_fraction')::numeric > (vals->>'capital.pool_fraction')::numeric
       OR (vals->>'sizing.risk_fraction')::numeric > (vals->>'portfolio_risk.max_fraction')::numeric
       OR (vals->>'sizing.min_allocation')::numeric > (vals->>'sizing.max_allocation')::numeric
       THEN RAISE EXCEPTION 'POLICY_SAFETY_CONSTRAINT'; END IF;
    IF vals = s.payload->'values' THEN RAISE EXCEPTION 'POLICY_NO_CHANGE'; END IF;
    new_payload := jsonb_build_object('values',vals,'reason',p_document->>'reason');
    UPDATE v2_business_state SET version=s.version+1,payload=new_payload WHERE state_id=s.state_id;
    INSERT INTO v2_state_history(event_id,state_id,request_key,expected_version,version,payload,deleted,reason)
      VALUES(gen_random_uuid(),s.state_id,'web-policy:' || p_request::text,s.version,s.version+1,new_payload,false,'WEB_RUNTIME_POLICY_ACTIVATED');
    result := jsonb_build_object('registry_id',p_registry,'environment','SANDBOX','version',s.version+1,
      'previous_version',s.version,'execution_authorized',false,'status','APPLIED');
    INSERT INTO v2_policy_console_events(tenant_id,registry_id,request_id,expected_version,request,result)
      VALUES(p_tenant,p_registry,p_request,p_expected,p_document,result);
    RETURN result;
END;
$fn$;
$ddl$,ns);
EXECUTE format('REVOKE ALL ON FUNCTION %I.v2_web_policy_patch(uuid,uuid,bigint,uuid,jsonb) FROM PUBLIC',ns);
END;
$migration$;
