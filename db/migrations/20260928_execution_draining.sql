ALTER TABLE v2_execution_routes DROP CONSTRAINT v2_execution_routes_phase_check;
ALTER TABLE v2_execution_routes DROP CONSTRAINT v2_execution_routes_check;
ALTER TABLE v2_execution_routes ADD CONSTRAINT v2_execution_routes_phase_check
CHECK(phase IN ('REQUESTED','BLOCKED','ACTIVE','DRAINING','STOPPED'));
ALTER TABLE v2_execution_routes ADD CONSTRAINT v2_execution_routes_worker_check
CHECK((phase IN ('ACTIVE','DRAINING'))=(worker_token IS NOT NULL));
