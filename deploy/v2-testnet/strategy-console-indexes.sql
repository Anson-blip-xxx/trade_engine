-- Run with psql outside a transaction. Additive/nonblocking; no business rows changed.
-- If an interrupted build leaves an invalid index, inspect it explicitly before retrying.
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_receipts_analysis_recent
  ON trade_v2.v2_signal_receipts(consumer,completed_at DESC,signal_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_decisions_analysis_recent
  ON trade_v2.v2_strategy_decisions(consumer,created_at DESC,decision_id);
