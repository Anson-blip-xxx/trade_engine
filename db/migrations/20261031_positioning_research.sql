-- Independent public research only. Deploy with a dedicated role granted SELECT
-- on the required public catalog/current-scope views, and SELECT/INSERT/DELETE
-- on these observations (DELETE is for bounded 30-day retention only). No
-- credential reads, order/policy writes or execution privilege is granted here.
CREATE TABLE IF NOT EXISTS v2_positioning_observations (
 observation_id uuid PRIMARY KEY,
 source text NOT NULL CHECK(source='BINANCE_USDM_LIVE_PUBLIC'),
 symbol text NOT NULL CHECK(char_length(symbol) BETWEEN 5 AND 44 AND right(symbol,4)='USDT'),
 slot_ms bigint NOT NULL CHECK(slot_ms>=0 AND slot_ms<9007199254740992 AND slot_ms%300000=0),
 method_version text NOT NULL CHECK(method_version='positioning-observation-v1'),
 bundle jsonb NOT NULL CHECK(jsonb_typeof(bundle)='object'),
 features jsonb NOT NULL CHECK((jsonb_typeof(features)='object'
   AND features->'execution_authorized'='false'::jsonb
   AND features->'automatic_parameter_updates'='false'::jsonb) IS TRUE),
 membership jsonb NOT NULL CHECK(jsonb_typeof(membership)='array' AND jsonb_array_length(membership) BETWEEN 1 AND 3),
 input_digest text NOT NULL CHECK(input_digest ~ '^[0-9a-f]{64}$'),
 first_received_at_ms bigint NOT NULL DEFAULT floor(extract(epoch FROM clock_timestamp())*1000)::bigint,
 CHECK((first_received_at_ms >= (bundle->>'finished_at_ms')::bigint) IS TRUE),
 CHECK(octet_length(bundle::text)+octet_length(features::text)+octet_length(membership::text)<=262144),
 UNIQUE(source,symbol,slot_ms,method_version)
);
CREATE INDEX IF NOT EXISTS v2_positioning_symbol_recent
 ON v2_positioning_observations(source,symbol,method_version,slot_ms DESC)
 INCLUDE(first_received_at_ms);
CREATE INDEX IF NOT EXISTS v2_positioning_retention
 ON v2_positioning_observations(first_received_at_ms,observation_id);
CREATE TABLE IF NOT EXISTS v2_positioning_runs (
 run_id uuid PRIMARY KEY,
 method_version text NOT NULL CHECK(method_version='positioning-observation-v1'),
 started_at_ms bigint NOT NULL CHECK(started_at_ms>=0 AND started_at_ms<9007199254740992),
 completed_at_ms bigint NOT NULL CHECK(completed_at_ms>=started_at_ms AND completed_at_ms<=started_at_ms+3600000),
 status text NOT NULL CHECK(status IN ('COMPLETE','COMPLETED','PARTIAL','FAILED','NO_CATALOG','NO_SYMBOLS','RATE_LIMITED','SKIPPED','DEFERRED')),
 coverage jsonb NOT NULL CHECK(jsonb_typeof(coverage)='object' AND octet_length(coverage::text)<=16384),
 error_code text CHECK(error_code ~ '^[A-Z][A-Z0-9_]{0,79}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS v2_positioning_runs_recent
 ON v2_positioning_runs(method_version,completed_at_ms DESC);
CREATE OR REPLACE FUNCTION v2_positioning_reject_update() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'POSITIONING_FIRST_OBSERVATION_IMMUTABLE'; END $$;
DROP TRIGGER IF EXISTS v2_positioning_observations_immutable ON v2_positioning_observations;
CREATE TRIGGER v2_positioning_observations_immutable BEFORE UPDATE ON v2_positioning_observations
 FOR EACH ROW EXECUTE FUNCTION v2_positioning_reject_update();
DROP TRIGGER IF EXISTS v2_positioning_runs_immutable ON v2_positioning_runs;
CREATE TRIGGER v2_positioning_runs_immutable BEFORE UPDATE ON v2_positioning_runs
 FOR EACH ROW EXECUTE FUNCTION v2_positioning_reject_update();
