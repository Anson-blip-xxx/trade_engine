-- Run in the dedicated research schema, owned by an administrator.
CREATE TABLE IF NOT EXISTS actual_exit_studies (
 study_id text PRIMARY KEY, config_json text NOT NULL,
 registered_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS actual_exit_jobs (
 study_id text NOT NULL REFERENCES actual_exit_studies(study_id),
 intent_id uuid NOT NULL,
 status text NOT NULL CHECK(status IN ('PENDING','DONE','UNCOVERED')),
 attempts integer NOT NULL DEFAULT 1,
 first_attempt_ms bigint NOT NULL,
 retry_after_ms bigint NOT NULL,
 payload_json text NOT NULL CHECK(octet_length(payload_json)<=2000000),
 content_digest text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(study_id,intent_id)
);
CREATE INDEX IF NOT EXISTS actual_exit_retention ON actual_exit_jobs(created_at);
CREATE TABLE IF NOT EXISTS exit_funnel_audits (
 source_digest text NOT NULL, version text NOT NULL,
 status text NOT NULL CHECK(status IN ('ANALYZED','UNCOVERED')),
 payload_json text NOT NULL CHECK(octet_length(payload_json)<=2000000),
 content_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_digest,version)
);
