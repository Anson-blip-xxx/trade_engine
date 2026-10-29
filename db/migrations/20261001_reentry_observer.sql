-- Additive research storage only. No business-state writes or order authority.
CREATE TABLE v2_reentry_observer_state (
    name TEXT PRIMARY KEY CHECK (name='sandbox'),
    started_ms BIGINT NOT NULL CHECK (started_ms>0),
    heartbeat_ms BIGINT NOT NULL DEFAULT 0,
    health JSONB NOT NULL DEFAULT '{}',
    total_bytes BIGINT NOT NULL DEFAULT 0 CHECK (total_bytes>=0),
    CHECK (octet_length(health::text)<=8192)
);
CREATE TABLE v2_reentry_observer_budget (
    day DATE PRIMARY KEY,
    rows_used INTEGER NOT NULL CHECK (rows_used>=0),
    bytes_used BIGINT NOT NULL CHECK (bytes_used>=0)
);
CREATE TABLE v2_reentry_observations (
    decision_id UUID NOT NULL,
    policy_digest TEXT NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    registry_id UUID NOT NULL,
    scope JSONB NOT NULL,
    route_epoch BIGINT NOT NULL,
    decided_ms BIGINT NOT NULL,
    observed_ms BIGINT NOT NULL CHECK (observed_ms>=decided_ms),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    status TEXT NOT NULL CHECK (status IN ('READY','WAIT','UNKNOWN','NOT_APPLICABLE')),
    reason TEXT NOT NULL,
    claim TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY(decision_id,policy_digest),
    CHECK (scope->>'environment'='SANDBOX'),
    CHECK (payload->>'execution_authorized'='false'),
    CHECK (octet_length(payload::text)<=262144)
);
CREATE INDEX v2_reentry_observations_account ON v2_reentry_observations(registry_id,observed_ms DESC);
CREATE TRIGGER v2_reentry_observations_immutable BEFORE UPDATE OR DELETE ON v2_reentry_observations
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_reentry_observer_claims (
    identity TEXT PRIMARY KEY CHECK (identity ~ '^[0-9a-f]{64}$'),
    decision_id UUID NOT NULL,
    policy_digest TEXT NOT NULL,
    FOREIGN KEY(decision_id,policy_digest) REFERENCES v2_reentry_observations(decision_id,policy_digest)
      DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER v2_reentry_observer_claims_immutable BEFORE UPDATE OR DELETE ON v2_reentry_observer_claims
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
