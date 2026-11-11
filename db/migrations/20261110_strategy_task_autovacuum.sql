-- Run in a transaction with an explicit schema/search_path and a short
-- lock_timeout (100ms live). Retry later if busy; never wait behind trading.
-- High-churn queue over retained history: the default 20% threshold waited
-- for ~1.9M dead rows while the pending-only index grew to 315 MiB.
-- Only this table's trigger changes. Inherit normal vacuum I/O throttling;
-- do not vacuum FULL, delete task history, change leases, or alter strategy.
ALTER TABLE v2_strategy_tasks SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_vacuum_threshold = 10000
);
