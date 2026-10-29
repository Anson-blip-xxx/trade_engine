-- Research only: no changes to orders, balances, risk rules or execution routes.
CREATE TABLE IF NOT EXISTS v2_daily_mover_days (
 day date NOT NULL, method_version text NOT NULL,
 status text NOT NULL CHECK(status IN ('COLLECTING','PARTIAL','COMPLETE')),
 catalog jsonb NOT NULL, catalog_observed_at_ms bigint NOT NULL,
 ranking jsonb, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(day,method_version)
);
CREATE TABLE IF NOT EXISTS v2_daily_mover_symbols (
 day date NOT NULL, method_version text NOT NULL, symbol text NOT NULL,
 status text NOT NULL CHECK(status IN ('COMPLETE','UNKNOWN')),
 summary jsonb, hourly jsonb, minute_path jsonb,
 error_code text, attempts integer NOT NULL DEFAULT 1,
 path_attempts integer NOT NULL DEFAULT 0,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(day,method_version,symbol),
 FOREIGN KEY(day,method_version) REFERENCES v2_daily_mover_days(day,method_version)
);
ALTER TABLE v2_daily_mover_symbols ADD COLUMN IF NOT EXISTS path_attempts integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS v2_daily_mover_raw_retention
 ON v2_daily_mover_symbols(day) WHERE minute_path IS NOT NULL;
CREATE TABLE IF NOT EXISTS v2_daily_mover_reports (
 report_id uuid PRIMARY KEY,
 day date NOT NULL, method_version text NOT NULL, scope_key text NOT NULL,
 source_digest text NOT NULL, report jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(day,method_version,scope_key,source_digest)
);
CREATE INDEX IF NOT EXISTS v2_daily_mover_reports_recent
 ON v2_daily_mover_reports(scope_key,day DESC,created_at DESC);
CREATE TABLE IF NOT EXISTS v2_daily_mover_delivery (
 report_id uuid PRIMARY KEY REFERENCES v2_daily_mover_reports(report_id),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','SENDING','SENT','UNKNOWN')),
 attempts integer NOT NULL DEFAULT 0, attempted_at timestamptz,
 message_id bigint, sent_at timestamptz
);
-- Compact daily features/reports persist; optional raw minute paths expire.
-- Cleanup is bounded and touches only the new research tables.
CREATE OR REPLACE FUNCTION v2_daily_mover_report_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'DAILY_MOVER_REPORT_IMMUTABLE'; END $$;
DROP TRIGGER IF EXISTS v2_daily_mover_report_immutable ON v2_daily_mover_reports;
CREATE TRIGGER v2_daily_mover_report_immutable BEFORE UPDATE OR DELETE
 ON v2_daily_mover_reports FOR EACH ROW EXECUTE FUNCTION v2_daily_mover_report_immutable();
