-- Opt-in after 20260928_strategy_console.sql. No blanket state/order writes.
GRANT SELECT ON trade_v2.v2_business_state,trade_v2.v2_state_history,
  trade_v2.v2_policy_console_events,trade_v2.v2_decision_evidence,
  trade_v2.v2_strategy_decisions,trade_v2.v2_signal_receipts,
  trade_v2.v2_inbound_signals,trade_v2.v2_orders,trade_v2.v2_fills,
  trade_v2.v2_exchange_income TO tradev2accounts;
GRANT EXECUTE ON FUNCTION trade_v2.v2_web_policy_patch(uuid,uuid,bigint,uuid,jsonb) TO tradev2accounts;
-- Existing account-admin grants already provide registry/intent/settlement reads.
-- Intentionally no INSERT/UPDATE/DELETE grants on generic state or order tables.
