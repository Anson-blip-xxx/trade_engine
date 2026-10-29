-- Dedicated research schema only. No grants on trading/account/credential tables.
CREATE TABLE IF NOT EXISTS exit_studies (
 study_id text PRIMARY KEY,
 config_json text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS exit_cursors (
 study_id text NOT NULL REFERENCES exit_studies(study_id),
 symbol text NOT NULL,
 next_start_ms bigint NOT NULL,
 PRIMARY KEY(study_id,symbol)
);
CREATE TABLE IF NOT EXISTS exit_samples (
 study_id text NOT NULL REFERENCES exit_studies(study_id),
 symbol text NOT NULL,
 start_ms bigint NOT NULL,
 mode text NOT NULL CHECK(mode IN ('FORWARD','CALIBRATION')),
 status text NOT NULL CHECK(status IN ('SIMULATED','HISTORY_GAP')),
 payload_json text NOT NULL CHECK(octet_length(payload_json)<=2000000),
 content_digest text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(study_id,symbol,start_ms,mode)
);
CREATE INDEX IF NOT EXISTS exit_samples_retention ON exit_samples(created_at);
