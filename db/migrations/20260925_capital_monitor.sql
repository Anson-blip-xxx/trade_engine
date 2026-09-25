-- Internal scoped monitor. Apply after capital_transfers; no runner activation.
CREATE TABLE v2_capital_monitor_state (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    version BIGINT NOT NULL CHECK(version>0),
    payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
    PRIMARY KEY(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TABLE v2_capital_monitor_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('OPEN','REMINDER','RECOVERED')),
    payload JSONB NOT NULL CHECK(jsonb_typeof(payload)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE INDEX v2_capital_monitor_pending ON v2_capital_monitor_events(tenant_id,registry_id,sequence);
CREATE TRIGGER v2_capital_monitor_events_immutable BEFORE UPDATE OR DELETE ON v2_capital_monitor_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_capital_monitor_delivery (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    destination_ref UUID NOT NULL,
    last_sequence BIGINT NOT NULL DEFAULT 0,
    next_attempt_ms BIGINT NOT NULL DEFAULT 0,
    last_attempt_ms BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
