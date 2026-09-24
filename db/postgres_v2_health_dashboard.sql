-- Additive migration. Execute with search_path set to the isolated V2 schema.
-- Application roles are deployment-specific: grant SELECT on this view only.
CREATE OR REPLACE VIEW v2_business_health_dashboard WITH (security_barrier=true) AS
SELECT scope->>'account_id' AS account_id,payload
FROM v2_business_state WHERE NOT deleted
  AND scope->>'namespace'='trading-health-v1' AND scope->>'key'='latest'
  AND scope->>'environment'='SANDBOX' AND scope->>'exchange'='BINANCE'
  AND scope->>'product'='FUTURES';
