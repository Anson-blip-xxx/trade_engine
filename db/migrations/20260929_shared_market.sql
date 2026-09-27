-- Shared market facts only; no account credentials, positions or execution grants.
CREATE TABLE v2_market_catalog (
 environment text PRIMARY KEY CHECK(environment IN ('SANDBOX','LIVE')),
 observed_at_ms bigint NOT NULL, digest text NOT NULL, contracts jsonb NOT NULL
);
CREATE TABLE v2_market_symbol_heads (
 environment text NOT NULL, symbol text NOT NULL,
 observed_at_ms bigint NOT NULL DEFAULT 0, attempted_at_ms bigint NOT NULL DEFAULT 0,
 status text NOT NULL, reason text, frame_id text,
 PRIMARY KEY(environment,symbol)
);
CREATE INDEX v2_market_heads_due ON v2_market_symbol_heads(environment,attempted_at_ms);
CREATE TABLE v2_market_hub_status (
 environment text PRIMARY KEY, observed_at_ms bigint NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE v2_egress_budgets (
 host text PRIMARY KEY, window_id bigint NOT NULL DEFAULT 0,
 used_weight integer NOT NULL DEFAULT 0, market_weight integer NOT NULL DEFAULT 0,
 blocked_until timestamptz NOT NULL DEFAULT '-infinity'
);
CREATE TABLE v2_market_demands (
 environment text NOT NULL CHECK(environment IN ('SANDBOX','LIVE')),
 symbol text NOT NULL, expires_at_ms bigint NOT NULL,
 PRIMARY KEY(environment,symbol)
);
CREATE INDEX v2_market_signal_demand ON v2_inbound_signals(environment,source,received_at DESC);
