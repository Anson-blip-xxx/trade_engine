CREATE TABLE IF NOT EXISTS v2_market_candles_incremental (
 environment LowCardinality(String), symbol LowCardinality(String), open_ms UInt64,
 payload String, content_digest FixedString(64)
) ENGINE=MergeTree
PARTITION BY toYYYYMM(toDateTime(open_ms/1000))
ORDER BY (environment,symbol,open_ms);
