-- Internal SANDBOX monitoring control; never enables trading.
CREATE TABLE v2_capital_monitor_controls (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    version BIGINT NOT NULL CHECK(version>0),
    enabled BOOLEAN NOT NULL,
    policy JSONB NOT NULL CHECK(jsonb_typeof(policy)='object'),
    PRIMARY KEY(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TABLE v2_capital_monitor_control_events (
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    request_id UUID NOT NULL,
    request_digest TEXT NOT NULL,
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,request_id)
);
CREATE TRIGGER v2_capital_monitor_controls_audit_immutable BEFORE UPDATE OR DELETE ON v2_capital_monitor_control_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_capital_monitor_runs (
    run_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    control_version BIGINT NOT NULL,
    observed_at_ms BIGINT NOT NULL CHECK(observed_at_ms>=0),
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE INDEX v2_capital_monitor_latest_run ON v2_capital_monitor_runs(tenant_id,registry_id,observed_at_ms DESC);
CREATE TRIGGER v2_capital_monitor_runs_immutable BEFORE UPDATE OR DELETE ON v2_capital_monitor_runs
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_capital_monitor_schedule (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    control_version BIGINT NOT NULL,
    observed_at_ms BIGINT NOT NULL,
    next_run_ms BIGINT NOT NULL,
    PRIMARY KEY(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
