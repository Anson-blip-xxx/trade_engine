-- Create matching Unix/PG role separately. No schema ownership or function grants.
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2shadow;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2shadow;
GRANT SELECT ON trade_v2.v2_execution_routes,trade_v2.v2_tenant_accounts,
 trade_v2.v2_account_retirements,trade_v2.v2_strategy_decisions,
 trade_v2.v2_decision_evidence,trade_v2.v2_trade_intents,trade_v2.v2_orders,
 trade_v2.v2_fills,trade_v2.v2_settlements,trade_v2.v2_directional_followups TO tradev2shadow;
GRANT SELECT,INSERT ON trade_v2.v2_reentry_observations,trade_v2.v2_reentry_observer_claims TO tradev2shadow;
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_reentry_observer_state,trade_v2.v2_reentry_observer_budget TO tradev2shadow;
GRANT SELECT ON trade_v2.v2_reentry_observations,trade_v2.v2_reentry_observer_state TO tradev2accounts;
