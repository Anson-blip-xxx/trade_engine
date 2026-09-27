ALTER TABLE v2_execution_routes DROP CONSTRAINT v2_execution_routes_phase_check;
ALTER TABLE v2_execution_routes DROP CONSTRAINT v2_execution_routes_worker_check;
ALTER TABLE v2_execution_routes ADD CONSTRAINT v2_execution_routes_phase_check
CHECK(phase IN ('REQUESTED','BLOCKED','STARTING','ACTIVE','DRAINING','STOPPED'));
ALTER TABLE v2_execution_routes ADD CONSTRAINT v2_execution_routes_worker_check
CHECK((phase IN ('STARTING','ACTIVE','DRAINING'))=(worker_token IS NOT NULL));
CREATE TABLE v2_execution_controller_events (
    event_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    environment TEXT NOT NULL CHECK(environment='SANDBOX'),
    epoch BIGINT NOT NULL,
    target_registry UUID NOT NULL,
    phase TEXT NOT NULL,
    blockers JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER v2_execution_controller_events_immutable BEFORE UPDATE OR DELETE ON v2_execution_controller_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
