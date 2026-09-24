# Signal freshness and business health

## Incident and correction

The 2026-09-24 Testnet investigation found two independent admission blockers:
external-symbol funding prevented settlement (fixed in `0a9f1de`), then FIFO
discovery consumed historical expired signals ahead of current 90-second signals.
Absence of orders was not proof that all current market signals failed strategy rules.

Scheduler discovery and claiming now prefer saved-decision recovery, then valid
current signals, then expired/future signals. Admission consumes up to 20 signals
per source/strategy each cycle, covering the current 19-symbol S3 frame without
inheriting the order-recovery batch of 10. Larger universes require remeasuring
cycle latency and sizing this budget; it is not unlimited fan-out.
Every pipeline cycle expires up to
1,000 never-decided stale signals per consumer/source, even when opening is
blocked. Safety/recovery stages run first. Expiry does not read market context,
submit orders, delete raw signals or alter existing decisions/intents. Signal row
locks, a second-snapshot recheck, immutable receipts and task completion preserve
idempotency; live leases are excluded. No strength/risk/position-size rule is relaxed.

## Independent checks

The systemd watchdog evaluates the following per Binance Testnet account:

| Diagnostic | Condition |
| --- | --- |
| ORDER_PROGRESS_STALLED | SUBMITTING/UNKNOWN or market ACKNOWLEDGED unchanged >60s; PREPARED >90s |
| SETTLEMENT_OVERDUE | Actual opening fills, zero ledger exposure, ACTIVE episode, last fill >180s |
| SIGNAL_CONSUMPTION_LAG | S3 / enabled TradingView input has no consumer receipt after 30s |
| PIPELINE_ENTRY_BLOCKED | Latest completed cycle remains ENTRY_BLOCKED for >120s |
| POSITION_SAFETY_BLOCKED | Protection/exit phase stays BLOCKED for >30s |
| BUSINESS_HEALTH_UNAVAILABLE | Inspection failed; never interpret as healthy |

Checks run approximately every 15s plus probe/delivery time. PostgreSQL state
`trading-health-v1/latest` stores account, first-seen timestamps, observations,
affected order/episode samples (up to 20), and per-source lag counts. The existing
state history retains observations. The read-only dashboard exposes these details
and labels missing or >60-second-old observations unknown. These are diagnostic
thresholds, not a promise that every trading risk is detected.

TG sends Chinese UTC+8 abnormal/recovered transitions without trace IDs.
Acknowledged categories persist in `watchdog-delivery-v1/latest` to suppress
normal-restart duplicates. Delivery failure retries while the condition persists.
Sending precedes acknowledgement: crash in between can produce duplicates, not
exactly-once delivery. A transient condition that clears while TG is unavailable
remains in PG observation history but is not guaranteed a delayed TG notification.
Unknown business/PG status cannot clear a previously reported business incident.

## Operations and remaining boundaries

Do not auto-resubmit an UNKNOWN order or bypass safety because it is overdue.
Inspect exchange evidence and the saved order identity first. A legitimate long
resting LIMIT order is not treated as a missing market-order fill. Watching order
updated_at detects inactivity, not total time under continual status changes.
Signal delay can be an intentional consequence of a safety block; inspect both
categories together. No signal for a long period is not itself a market-feed alarm;
market failures remain covered by pipeline/dependency checks.

An explicit `ACCOUNT_RISK_CAPACITY_UNAVAILABLE` or `EXISTING_SYMBOL_POSITION`
ValueError is a DEFERRED admission, not a pipeline dependency failure. The task
retains its reason/backoff and expires normally; no order or decision is invented.
Health summaries list these in `expected_waits`, separately from unexplained lag.
The deployed Testnet policy on 2026-09-24 allows one position and 100 USDT total
notional; a filled 99.99486 USDT position legitimately leaves no second slot.
This release does not change that policy. Missing/invalid risk evidence remains
UNAVAILABLE and must not be mislabeled a normal capacity wait.
Capacity is checked before exchange/context requests to avoid wasting calls and
delaying protection cycles; the final context check and atomic submission risk
reservation are retained to guard concurrent changes.

No opening frequency or profit is guaranteed. Once current signals are caught up,
measure strategy rejection reasons and coverage before changing thresholds.
This release does not expand the trading universe or permit LIVE orders.
Watchdog deployment must accompany daemon deployment; dashboard release must also
be updated to expose the new observations. Preserve previous immutable releases
for rollback. Apply the additive `db/postgres_v2_health_dashboard.sql` migration
with the isolated V2 schema search_path and grant the dashboard role SELECT on
`v2_business_health_dashboard` only. Do not grant access to all business state.
No trading-table migration or historical-data deletion is required.
