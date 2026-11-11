-- Apply only in the isolated V2 schema. No trading table or policy is changed.
CREATE TABLE IF NOT EXISTS v2_trend_campaign_reports (
 method_version text NOT NULL CHECK (method_version='sandbox-trend-campaign-research-v1'),
 policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
 scope_digest text NOT NULL CHECK (scope_digest ~ '^[0-9a-f]{64}$'),
 episode_digest text NOT NULL CHECK (episode_digest ~ '^[0-9a-f]{64}$'),
 facts_digest text NOT NULL CHECK (facts_digest ~ '^[0-9a-f]{64}$'),
 input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
 report_digest text NOT NULL CHECK (report_digest ~ '^[0-9a-f]{64}$'),
 status text NOT NULL CHECK (status IN ('PENDING','UNKNOWN','ANALYZED','INELIGIBLE','NOT_APPLICABLE')),
 ready_at_ms bigint CHECK (ready_at_ms>=0 AND (status<>'PENDING' OR ready_at_ms IS NOT NULL)),
 report jsonb NOT NULL CHECK (
   jsonb_typeof(report)='object' AND octet_length(report::text)<=262144
   AND report ?& ARRAY['environment','execution_authorized','automatic_parameter_updates',
     'promotion_authorized','status','method_version','policy_digest','scope_digest',
     'episode_digest','facts_digest','input_digest','report_digest']
   AND (report->>'environment') IS NOT DISTINCT FROM 'SANDBOX'
   AND report->'execution_authorized'='false'::jsonb
   AND report->'automatic_parameter_updates'='false'::jsonb
   AND report->'promotion_authorized'='false'::jsonb
   AND (report->>'status') IS NOT DISTINCT FROM status
   AND (report->>'method_version') IS NOT DISTINCT FROM method_version
   AND (report->>'policy_digest') IS NOT DISTINCT FROM policy_digest
   AND (report->>'scope_digest') IS NOT DISTINCT FROM scope_digest
   AND (report->>'episode_digest') IS NOT DISTINCT FROM episode_digest
   AND (report->>'facts_digest') IS NOT DISTINCT FROM facts_digest
   AND (report->>'input_digest') IS NOT DISTINCT FROM input_digest
   AND (report->>'report_digest') IS NOT DISTINCT FROM report_digest
 ),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(method_version,policy_digest,episode_digest,input_digest)
);
CREATE INDEX IF NOT EXISTS v2_trend_campaign_scope_history
 ON v2_trend_campaign_reports(method_version,policy_digest,scope_digest,created_at DESC);
REVOKE ALL ON v2_trend_campaign_reports FROM PUBLIC;
-- Append-only evidence. A distinct corrected input/revision inserts a new row;
-- neither retry nor a later conclusion is allowed to overwrite old evidence.
CREATE OR REPLACE FUNCTION v2_trend_campaign_reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'TREND_CAMPAIGN_REPORTS_APPEND_ONLY';
END $$;
DROP TRIGGER IF EXISTS v2_trend_campaign_append_only ON v2_trend_campaign_reports;
CREATE TRIGGER v2_trend_campaign_append_only
 BEFORE UPDATE OR DELETE ON v2_trend_campaign_reports
 FOR EACH ROW EXECUTE FUNCTION v2_trend_campaign_reject_mutation();
