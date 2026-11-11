-- Optional coverage research only. Apply separately as cluster owner.
-- No account data, credentials, catalog mutation, or execution rights.
GRANT SELECT(environment,observed_at_ms,contracts)
 ON trade_v2.v2_market_catalog TO tradev2movers;
