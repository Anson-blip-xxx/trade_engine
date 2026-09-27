-- Desired and acknowledged execution state are separate. No implicit activation.
CREATE TABLE v2_execution_routes (
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    environment TEXT NOT NULL CHECK(environment IN ('SANDBOX','LIVE')),
    epoch BIGINT NOT NULL CHECK(epoch>0),
    target_registry UUID NOT NULL,
    binding_version BIGINT NOT NULL CHECK(binding_version>0),
    phase TEXT NOT NULL CHECK(phase IN ('REQUESTED','BLOCKED','ACTIVE','STOPPED')),
    worker_token UUID,
    blockers JSONB NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(blockers)='array'),
    PRIMARY KEY(tenant_id,environment),
    FOREIGN KEY(tenant_id,target_registry) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    CHECK((phase='ACTIVE')=(worker_token IS NOT NULL))
);
CREATE TABLE v2_execution_route_events (
    event_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    environment TEXT NOT NULL,
    epoch BIGINT NOT NULL,
    request_id UUID NOT NULL,
    operation TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(tenant_id,request_id),
    FOREIGN KEY(tenant_id) REFERENCES v2_tenants(tenant_id),
    CHECK((result->>'phase'='BLOCKED' AND result->'execution_authorized'='false'::jsonb) IS TRUE)
);
CREATE TRIGGER v2_execution_route_events_immutable BEFORE UPDATE OR DELETE ON v2_execution_route_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
