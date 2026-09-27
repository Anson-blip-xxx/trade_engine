-- Dedicated peer-authenticated owner console; no execution/ledger mutation grants.
CREATE ROLE tradev2accounts LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2accounts;
GRANT USAGE ON SCHEMA trade_v2 TO tradev2accounts;
GRANT SELECT ON trade_v2.v2_tenants, trade_v2.v2_trade_intents, trade_v2.v2_settlements TO tradev2accounts;
GRANT SELECT, INSERT ON trade_v2.v2_tenant_accounts, trade_v2.v2_registry_events, trade_v2.v2_credential_vault, trade_v2.v2_account_alias_events TO tradev2accounts;
GRANT SELECT, INSERT, UPDATE ON trade_v2.v2_account_aliases TO tradev2accounts;
