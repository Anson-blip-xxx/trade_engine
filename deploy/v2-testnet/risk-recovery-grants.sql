-- Web role can authorize only the validated Testnet manual probe function.
GRANT SELECT ON trade_v2.v2_risk_recovery_events TO tradev2accounts;
GRANT EXECUTE ON FUNCTION trade_v2.v2_web_risk_recovery_check(uuid,uuid),
  trade_v2.v2_web_risk_recovery_apply(uuid,uuid,uuid,jsonb) TO tradev2accounts;
-- No generic state UPDATE, no risk policy mutation, no order writes.
