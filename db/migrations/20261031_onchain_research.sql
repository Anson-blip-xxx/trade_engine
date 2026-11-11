-- Independent on-chain observation lane. The runtime role must have no order,
-- policy or credential write/read privileges. Registry INSERT belongs to a
-- separate deployment role; the observer reads only verified registry entries.
CREATE TABLE IF NOT EXISTS v2_onchain_mappings (
 mapping_id uuid PRIMARY KEY,
 provider text NOT NULL CHECK(provider='DEXPAPRIKA'),
 symbol text NOT NULL CHECK(symbol ~ '^[A-Z0-9_]{1,40}USDT$'),
 method_version text NOT NULL CHECK(method_version='onchain-asset-map-v1'),
 verified_at_ms bigint NOT NULL CHECK(verified_at_ms>=0 AND verified_at_ms<9007199254740992),
 valid_until_ms bigint NOT NULL CHECK(valid_until_ms>verified_at_ms),
 status text NOT NULL CHECK(status IN ('VERIFIED','UNSUPPORTED','REVOKED')),
 mapping jsonb NOT NULL CHECK(jsonb_typeof(mapping)='object' AND octet_length(mapping::text)<=16384),
 input_digest text NOT NULL CHECK(input_digest ~ '^[0-9a-f]{64}$'),
 first_received_at_ms bigint NOT NULL DEFAULT floor(extract(epoch FROM clock_timestamp())*1000)::bigint,
 CHECK(first_received_at_ms>=verified_at_ms),
 UNIQUE(provider,symbol,method_version,verified_at_ms)
);
CREATE INDEX IF NOT EXISTS v2_onchain_mappings_recent
 ON v2_onchain_mappings(provider,symbol,method_version,verified_at_ms DESC,first_received_at_ms DESC);

CREATE TABLE IF NOT EXISTS v2_onchain_observations (
 observation_id uuid PRIMARY KEY,
 provider text NOT NULL CHECK(provider='DEXPAPRIKA'),
 source text NOT NULL CHECK(source='DEXPAPRIKA_PUBLIC_KEYLESS'),
 symbol text NOT NULL CHECK(symbol ~ '^[A-Z0-9_]{1,40}USDT$'),
 slot_ms bigint NOT NULL CHECK(slot_ms>=0 AND slot_ms<9007199254740992 AND slot_ms%300000=0),
 method_version text NOT NULL CHECK(method_version='onchain-public-observation-v1'),
 mapping_id uuid REFERENCES v2_onchain_mappings(mapping_id),
 bundle jsonb NOT NULL CHECK(jsonb_typeof(bundle)='object'),
 features jsonb NOT NULL CHECK((jsonb_typeof(features)='object'
   AND features->'execution_authorized'='false'::jsonb
   AND features->'automatic_parameter_updates'='false'::jsonb
   AND features->'promotion_authorized'='false'::jsonb) IS TRUE),
 membership jsonb NOT NULL CHECK(membership IN ('["ACTIVE_SANDBOX_POSITION"]'::jsonb,'["EXPLICIT_RESEARCH_SMOKE"]'::jsonb)),
 input_digest text NOT NULL CHECK(input_digest ~ '^[0-9a-f]{64}$'),
 first_received_at_ms bigint NOT NULL DEFAULT floor(extract(epoch FROM clock_timestamp())*1000)::bigint,
 CHECK((first_received_at_ms >= (bundle->>'finished_at_ms')::bigint) IS TRUE),
 CHECK(octet_length(bundle::text)+octet_length(features::text)+octet_length(membership::text)<=262144),
 UNIQUE(provider,source,symbol,slot_ms,method_version)
);
CREATE INDEX IF NOT EXISTS v2_onchain_observations_recent
 ON v2_onchain_observations(provider,source,symbol,method_version,slot_ms DESC)
 INCLUDE(first_received_at_ms);
CREATE INDEX IF NOT EXISTS v2_onchain_observations_retention
 ON v2_onchain_observations(first_received_at_ms,observation_id);

CREATE TABLE IF NOT EXISTS v2_onchain_runs (
 run_id uuid PRIMARY KEY,
 method_version text NOT NULL CHECK(method_version='onchain-public-observation-v1'),
 started_at_ms bigint NOT NULL CHECK(started_at_ms>=0 AND started_at_ms<9007199254740992),
 completed_at_ms bigint NOT NULL CHECK(completed_at_ms>=started_at_ms AND completed_at_ms<=started_at_ms+3600000),
 status text NOT NULL CHECK(status IN ('COMPLETE','PARTIAL','FAILED','NO_SYMBOLS','IDLE','COOLDOWN','RATE_LIMITED','SKIPPED','DEFERRED')),
 coverage jsonb NOT NULL CHECK(jsonb_typeof(coverage)='object' AND octet_length(coverage::text)<=16384),
 error_code text CHECK(error_code ~ '^[A-Z][A-Z0-9_]{0,79}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS v2_onchain_runs_recent
 ON v2_onchain_runs(method_version,completed_at_ms DESC);
CREATE INDEX IF NOT EXISTS v2_onchain_runs_retention ON v2_onchain_runs(created_at,run_id);

CREATE TABLE IF NOT EXISTS v2_onchain_provider_circuits (
 provider text PRIMARY KEY CHECK(provider='DEXPAPRIKA'),
 consecutive_failures integer NOT NULL CHECK(consecutive_failures BETWEEN 0 AND 100),
 retry_after_ms bigint NOT NULL CHECK(retry_after_ms>=0 AND retry_after_ms<9007199254740992),
 updated_at_ms bigint NOT NULL CHECK(updated_at_ms>=0 AND updated_at_ms<9007199254740992),
 outcome text NOT NULL CHECK(outcome IN ('SUCCESS','FAILURE'))
);

CREATE OR REPLACE FUNCTION v2_onchain_reject_update() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ONCHAIN_FIRST_RECORD_IMMUTABLE'; END $$;
DROP TRIGGER IF EXISTS v2_onchain_mappings_immutable ON v2_onchain_mappings;
CREATE TRIGGER v2_onchain_mappings_immutable BEFORE UPDATE OR DELETE ON v2_onchain_mappings
 FOR EACH ROW EXECUTE FUNCTION v2_onchain_reject_update();
DROP TRIGGER IF EXISTS v2_onchain_observations_immutable ON v2_onchain_observations;
CREATE TRIGGER v2_onchain_observations_immutable BEFORE UPDATE ON v2_onchain_observations
 FOR EACH ROW EXECUTE FUNCTION v2_onchain_reject_update();
DROP TRIGGER IF EXISTS v2_onchain_runs_immutable ON v2_onchain_runs;
CREATE TRIGGER v2_onchain_runs_immutable BEFORE UPDATE ON v2_onchain_runs
 FOR EACH ROW EXECUTE FUNCTION v2_onchain_reject_update();
