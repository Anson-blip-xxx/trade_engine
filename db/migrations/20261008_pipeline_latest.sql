-- QA/migration transaction. Live deployment uses the concurrent variant.
CREATE INDEX IF NOT EXISTS v2_pipeline_latest_account
ON v2_business_state ((scope->>'account_id'), ((payload->>'started_at_ms')::bigint) DESC)
WHERE NOT deleted AND scope->>'namespace'='trading-pipeline-cycle-v1'
AND payload->>'status' IN ('CYCLE_COMPLETE','ENTRY_BLOCKED');
