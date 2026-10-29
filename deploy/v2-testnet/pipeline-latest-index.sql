-- Execute outside a transaction. Indexes diagnostics only; never alters trades.
SET lock_timeout='2s';
SET statement_timeout='120s';
CREATE INDEX CONCURRENTLY IF NOT EXISTS v2_pipeline_latest_account
ON trade_v2.v2_business_state ((scope->>'account_id'), ((payload->>'started_at_ms')::bigint) DESC)
WHERE NOT deleted AND scope->>'namespace'='trading-pipeline-cycle-v1'
AND payload->>'status' IN ('CYCLE_COMPLETE','ENTRY_BLOCKED');
