-- Run as cluster owner after the dedicated OS/PG role and research schema exist.
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2movers;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2movers;
GRANT SELECT ON trade_v2.v2_inbound_signals,trade_v2.v2_strategy_decisions,
 trade_v2.v2_decision_evidence,trade_v2.v2_signal_receipts,trade_v2.v2_trade_intents,
 trade_v2.v2_orders,trade_v2.v2_fills,trade_v2.v2_domain_outbox,
 trade_v2.v2_order_events,trade_v2.v2_episodes,trade_v2.v2_settlements,
 trade_v2.v2_business_state TO tradev2movers;
GRANT SELECT(tenant_id,registry_id,exchange,account_id,environment,product,display_name,created_at)
 ON trade_v2.v2_tenant_accounts TO tradev2movers;
GRANT SELECT(tenant_id,target_registry,environment,phase)
 ON trade_v2.v2_execution_routes TO tradev2movers;
-- Shared public quota accounting is the only existing mutable infrastructure.
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_egress_budgets,
 trade_v2.v2_public_market_budgets TO tradev2movers;
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_daily_mover_days,
 trade_v2.v2_daily_mover_symbols,trade_v2.v2_daily_mover_delivery TO tradev2movers;
GRANT SELECT,INSERT ON trade_v2.v2_daily_mover_reports TO tradev2movers;
