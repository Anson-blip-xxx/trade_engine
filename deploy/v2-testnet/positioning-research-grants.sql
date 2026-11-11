-- Apply as owner to trade_v2_testnet only, after the two research migrations.
DO $$ BEGIN
 IF current_database()<>'trade_v2_testnet' THEN RAISE EXCEPTION 'WRONG_DATABASE'; END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='tradev2positioning') THEN
  CREATE ROLE tradev2positioning LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='tradev2campaign') THEN
  CREATE ROLE tradev2campaign LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
END $$;
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2positioning,tradev2campaign;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2positioning,tradev2campaign;
GRANT SELECT (catalog,ranking,catalog_observed_at_ms,day,method_version)
 ON trade_v2.v2_daily_mover_days TO tradev2positioning;
GRANT SELECT (intent_id,payload,exchange,account_id,environment,product)
 ON trade_v2.v2_trade_intents TO tradev2positioning;
GRANT SELECT (episode_id,status) ON trade_v2.v2_episodes TO tradev2positioning;
GRANT SELECT (order_id,episode_id,leg) ON trade_v2.v2_orders TO tradev2positioning;
GRANT SELECT (order_id,quantity) ON trade_v2.v2_fills TO tradev2positioning;
GRANT SELECT ON trade_v2.v2_business_state TO tradev2positioning;
GRANT SELECT (tenant_id,registry_id,version,exchange,account_id,environment,product)
 ON trade_v2.v2_tenant_accounts TO tradev2positioning;
GRANT SELECT (tenant_id,target_registry,binding_version,environment,phase)
 ON trade_v2.v2_execution_routes TO tradev2positioning;
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_egress_budgets,trade_v2.v2_public_market_budgets
 TO tradev2positioning;
GRANT SELECT,INSERT,DELETE ON trade_v2.v2_positioning_observations TO tradev2positioning;
GRANT SELECT,INSERT ON trade_v2.v2_positioning_runs TO tradev2positioning;
GRANT SELECT ON trade_v2.v2_trade_intents,trade_v2.v2_orders,trade_v2.v2_fills,
 trade_v2.v2_decision_evidence,trade_v2.v2_settlements,trade_v2.v2_directional_followups,
 trade_v2.v2_strategy_decisions,trade_v2.v2_episodes TO tradev2campaign;
GRANT SELECT,INSERT ON trade_v2.v2_trend_campaign_reports TO tradev2campaign;
