-- Internal Sandbox switch preparation. No target activation state exists yet.
CREATE TABLE v2_account_switches (
    switch_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    request_id UUID NOT NULL,
    source_registry UUID NOT NULL,
    target_registry UUID NOT NULL,
    source_version BIGINT NOT NULL CHECK(source_version>0),
    target_version BIGINT NOT NULL CHECK(target_version>0),
    version BIGINT NOT NULL CHECK(version>0),
    status TEXT NOT NULL CHECK(status IN ('REQUESTED','DRAINING','CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK(source_registry<>target_registry),
    UNIQUE(tenant_id,request_id),
    UNIQUE(tenant_id,switch_id),
    FOREIGN KEY(tenant_id,source_registry) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,target_registry) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TABLE v2_account_switch_claims (
    registry_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    switch_id UUID NOT NULL,
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,switch_id) REFERENCES v2_account_switches(tenant_id,switch_id)
);
CREATE TABLE v2_account_switch_events (
    event_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    switch_id UUID NOT NULL,
    version BIGINT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('REQUESTED','DRAINING','CANCELLED')),
    evidence JSONB NOT NULL CHECK(jsonb_typeof(evidence)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(switch_id,version),
    FOREIGN KEY(tenant_id,switch_id) REFERENCES v2_account_switches(tenant_id,switch_id)
);
CREATE TRIGGER v2_account_switch_events_immutable BEFORE UPDATE OR DELETE ON v2_account_switch_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE FUNCTION v2_guard_account_switch_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'switch history cannot be deleted'; END IF;
    IF (to_jsonb(NEW)-ARRAY['version','status']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['version','status'])
        OR NEW.version<>OLD.version+1 OR OLD.status<>'REQUESTED' OR NEW.status NOT IN ('DRAINING','CANCELLED')
    THEN RAISE EXCEPTION 'invalid switch transition'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_account_switch_update_guard BEFORE UPDATE OR DELETE ON v2_account_switches
FOR EACH ROW EXECUTE FUNCTION v2_guard_account_switch_update();
