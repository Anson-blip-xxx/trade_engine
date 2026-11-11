-- Research projection only: no order, position, route, policy or history writes.
-- Install in the isolated V2 schema. Grant SELECT only to the research worker.
CREATE VIEW v2_stagnation_frozen_exit_evidence WITH (security_barrier=true) AS
SELECT h.state_id,b.scope AS state_scope,h.version,h.created_at AS recorded_at,
       h.payload->'account_scope' AS account_scope,
       h.payload->>'episode' AS episode,
       h.payload->'facts' AS facts,h.payload->'decision' AS decision,
       h.payload->'policy' AS policy,h.payload->'observation' AS observation,
       h.payload->'native_protection' AS native_protection,
       h.payload->'hourly_history' AS hourly_history
FROM v2_state_history h JOIN v2_business_state b USING(state_id)
WHERE b.scope->>'namespace'='directional-exit-v1'
  AND b.scope->>'exchange'='BINANCE'
  AND b.scope->>'environment'='SANDBOX'
  AND b.scope->>'product'='FUTURES'
  AND NOT h.deleted;
REVOKE ALL ON v2_stagnation_frozen_exit_evidence FROM PUBLIC;
