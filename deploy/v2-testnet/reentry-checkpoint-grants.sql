-- Provision the matching Unix/PG role separately; no membership in trading roles.
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2reentry;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2reentry;
GRANT SELECT ON trade_v2.v2_execution_routes,trade_v2.v2_tenant_accounts,
 trade_v2.v2_account_retirements,trade_v2.v2_trade_intents,trade_v2.v2_episodes,
 trade_v2.v2_orders,trade_v2.v2_fills,trade_v2.v2_business_state TO tradev2reentry;
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_reentry_checkpoints,
 trade_v2.v2_reentry_preparation_attempts TO tradev2reentry;
-- Current execution DB role; migration ownership must not be assumed.
GRANT SELECT,INSERT,UPDATE ON trade_v2.v2_reentry_checkpoints TO ubuntu;
-- No DELETE, trade/order/cash writes, credential tables, or key credentials.
