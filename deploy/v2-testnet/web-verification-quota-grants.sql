-- Apply only after the quota migration and new account-admin code are ready.
GRANT EXECUTE ON FUNCTION trade_v2.v2_web_testnet_verify_permit() TO tradev2accounts;
GRANT EXECUTE ON FUNCTION trade_v2.v2_web_testnet_quota_cooldown(integer) TO tradev2accounts;
-- Neither the new nor old Web runtime should retain a budget reset capability.
REVOKE ALL ON trade_v2.v2_egress_budgets FROM tradev2accounts;
REVOKE ALL ON trade_v2.v2_public_market_budgets FROM tradev2accounts;
