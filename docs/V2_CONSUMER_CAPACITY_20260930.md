# Signal consumer capacity — 2026-09-30

Times in UTC+8. Scope: current Testnet account; no LIVE activation, forced
orders, signal TTL extension, risk relaxation, account switch or audit deletion.

## Measured diagnosis

- Host: 4 CPUs, 7.5 GiB RAM; initial available memory 4.9 GiB and sampled CPU
  idle around 40%. No active swap-in/out in the sample; disk approximately
  70 GiB free. These observations do not justify a hardware upgrade first.
- Historical inbox approximately 727k rows, tasks and receipts approximately
  1.83m each. Every discovery and progress query repeatedly included that
  history. Discovery took 2–4 seconds within an 8-second S6 share of the
  16-second total evaluation budget; S6 context reads took another 4–5 seconds.
- Baseline full cycles: 25–28 seconds. S6 frequently evaluated only 5–18 inputs
  per cycle while S8 processed many non-directional bookkeeping inputs.
- One five-minute arrival sample: 628 S3 signals, 213 long-actionable and
  28 short-actionable. Equal evaluation shares were inappropriate for that
  asymmetry. Priority does not imply that a signal meets trade risk conditions.
- Pre-release ten-minute receipt sample: S6 448 directional completions,
  322 EXPIRED, average receipt delay 66.27 seconds, P95 100.66 seconds;
  S8 40 completions, 12 EXPIRED, average 45.68 seconds, P95 99.95 seconds.
  This measures receipt completion minus ingress time, not order-fill latency.
- Concurrently observed MOVR opening refusals carried
  CAPITAL_SINGLE_TRADE_RISK_EXCEEDED in readiness evidence; these were genuine
  last-mile risk checks, not the queue failure. Later MOVR filled normally.

## Changes

1. New valid/ready 41 MiB `v2_signals_live_discovery` index on
   source/environment/expiry, including signal ID. Built CONCURRENTLY online.
2. Hot discovery checks all still-valid input using that index. Parameterized
   task probes prevent the optimizer turning the small live set into a hash
   of all historical tasks. Read-only production-scale EXPLAIN: approximately
   3,130 ms baseline versus 15 ms hot query, zero temporary blocks written by
   the hot query. Merely adding the index still took about 3,172 ms; the query
   shape correction was essential. Measurements are samples, not an SLA.
3. Full discovery remains on startup and every 60 seconds, including old
   decisions and expired audits. There is no received-at/high-water cursor:
   late commits cannot be silently skipped. Existing expiry, recovery,
   immutable receipts, task leases and idempotent admission remain in place.
4. Independent GETs inside account context use at most four threads. Both
   position observations still surround the entire read set; failed/incomplete
   reads cannot publish account evidence. Account balances are never cached.
   Per-request shared PG quotas and the final guarded submit are unchanged.
   Already-held symbols are rejected immediately after the first position read.
5. Evaluation shares follow indexed live actionable demand, excluding completed
   receipts, active leases and retry backoff. Weights have a floor of one and
   a configurable cap of 16. Other consumers and old-intent recovery retain a
   processing opportunity; TV ordering and immediate PREPARED dispatch remain.
   The total 16-second cooperative budget is unchanged. In-flight I/O may still
   overrun its share; deadlines are rechecked before execution as before.
6. Full historical progress counts are cached for 300 seconds, with an explicit
   observation timestamp and cache indicator. They are diagnostic, never a
   current-queue or order permission check. Real health monitoring stays live.

New bounded runtime settings (account-scoped PolicyStore):

| Setting | Default | Bounds |
|---|---:|---:|
| context.read_workers | 4 | 1–4 |
| scheduler.discovery_reconcile_seconds | 60 | 1–3600 |
| scheduler.progress_seconds | 300 | 1–3600 |
| scheduler.backlog_weighted_budget | true | boolean |
| scheduler.max_demand_weight | 16 | 1–1000 |

No stored risk policy was patched: version 6, 70% pool, batch 100, 16,000 ms
evaluation budget, TV-first and 30-second lag threshold remain. Resolving newly
introduced default settings changes the effective policy digest, not risk limits.

## QA and release boundary

- First candidate regression: 269 passed; two new test wrappers lacked their
  environment metadata. Fixed the wrappers; did not relax production checks.
- Index/hot discovery/parallel reads candidate: 423 passed in 278.45 seconds.
- Final candidate with demand weighting: 257 affected-path tests passed in
  238.14 seconds. These sets overlap; they are not 680 unique tests.
- Coverage: late committed live signals, cold expired reconciliation, consumer
  isolation, 1/4-thread bounds, position bracketing, partial-read failure,
  configuration validation, repeatable migration, progress cache timestamps,
  demand excluding leases/backoff/receipts, fairness, existing scheduler races,
  strategy idempotency, source replay, guarded execution and HTTP quota tests.
- All QA used disposable isolated PostgreSQL/Redis and mocked venue writes;
  no test credentials were copied to runtime. Relevant Ruff checks passed.
- Release `consumer-capacity-20260930-v1` is based on the running precision
  release plus four runtime modules and allowlisted schema/tests/runner files.
  Unrelated quant/UI/account workspace changes are excluded. Host maintenance
  and market release pointers are unchanged.

## Runtime acceptance

### First deployment: improvement, not full acceptance

Same binding restored through the formal owner API at epoch 29 / ACTIVE, with
no route blockers. The market service retained its PID and release. Warm actual
discovery measurements were 23–83 ms, versus seconds previously; S6 processed
18–36 items in observed cycles. Live-capacity weights were persisted per cycle.

However, cycles remained 24–31 seconds and queue findings oscillated rather
than staying clear. Post-17:27 directional receipt cohort: S6 371 completions,
43 EXPIRED, average 44.73 seconds, P95 65.88 seconds; S8 35 completions,
4 EXPIRED, average 26.13 seconds, P95 44.22 seconds. The sample length differs
from baseline and market traffic varies, so these are observations, not a
controlled performance guarantee. No forced order was used for acceptance.

The immutable per-phase journal located remaining overhead: one cycle spent
3.276 seconds in protection, 4.545 seconds in exits and 3.335 seconds in expiry
maintenance. Safety checks must remain; their independent reads can overlap.

### Second candidate

AccountCoverageAudit now resolves `context.read_workers` for each collection.
Its inventory keeps positions-before and positions-after sequential around all
independent GETs, retains before/after local-facts checks and scope validation,
and blocks on partial reads. No inventory is reused between protection/exit.
The general inventory default remains sequential; the Testnet coverage path
opts into the bounded setting. Switching the setting to 1 restores sequential
collection without a code change. Original timeout and quota gates remain.

The same bounded account-context batch now also includes leverage brackets
and book ticker; their original validator is retained. This removes another
two serial network waits. An existing position or full PG risk budget is
detected before those additional reads.

Inventory/protection/exit/settlement/readiness regression: 175 passed in
145.01 seconds. The additional adaptive-context fixture initially omitted its
required capital-budget prerequisite; production validation correctly rejected
it. The fixture was fixed instead of weakening the invariant. Final second
candidate regression: **126 passed in 113.61 seconds**, with the final runtime
files frozen for that run. Its release pointer is
`/opt/trade-engine-v2/releases/consumer-capacity-20260930-v2`.

### Final observation and explicit remaining work

The same Testnet binding resumed through the owner API at epoch 30 / ACTIVE /
no route blockers. Native protection and the shared market process were not
replaced. The deployment did not change stored risk/capital settings, reset
history or submit synthetic test orders.

The second deployed cohort (signals received and completed from 17:45 onward,
sample read around 17:49): S6 215 completions, 15 EXPIRED, average 34.63 seconds,
P95 65.25 seconds; S8 25 completions, 4 EXPIRED, average 30.28 seconds,
P95 71.06 seconds. This is substantial improvement but **not stable low-latency
acceptance**. Do not describe this incident as fully resolved.

An old BMT evaluation retained a valid lease through 17:48:23 after restart.
It later received an EXPIRED terminal receipt through normal recovery; no
manual lease clearing or expired-signal replay was used. Its historical lease
token may remain in the completed row, but is no longer a live lease.

At 17:51:04, health still reported SIGNAL_CONSUMPTION_LAG: 22 S6/S3 and 2 S8/S3
inputs; the current cycle also had an entry_dispatch finding. Account route
remained ACTIVE and market was CURRENT at 17:51:18. Recent repeated AGT
cancellations carried **CAPITAL_PORTFOLIO_RISK_EXCEEDED** in immutable order
events. The portfolio cap must not be relaxed to make these disappear.

Two unresolved architectural issues need a separate bounded follow-up:

1. Signal evaluation still shares a serialized cycle with protection, exits
   and dispatch. An expected portfolio rejection yields the cycle and can defer
   other consumers. Separate real queue congestion from expected risk waits;
   avoid repeatedly doing full work for candidates that cannot use the remaining
   risk budget, without declaring unverified capacity or inventing a trade permit.
2. Introduce independently budgeted read/evaluation work and a durable pending
   projection, retaining one guarded account execution owner. Verify late commits,
   recovery, crash leases, overload, quota exhaustion and cross-account isolation
   under measured burst traffic before claiming the 30-second lag target met.

Final host observation: 4.9 GiB available RAM; no evidence that purchasing more
memory alone will address those serialization/risk-wait issues. Full runtime
acceptance remains open even though the individual regression suites passed.

## Capacity policy

Compare actionable input rate with sustainable useful evaluation rate, not
total bookkeeping task count. Measure bursts and per-source waiting time.
Only consider more CPU/RAM after query/I/O waste is removed and sustained
resource saturation is demonstrated. Additional strategy evaluators would
still require bounded queues, lease-safe claims, shared quotas and serialized
per-account admission; adding order-writing workers is not a safe shortcut.
Historical reconciliation remains O(history) at a lower cadence; indefinitely
growing retention will eventually require a separate durable discovery
projection/archival design. This release does not claim unlimited capacity.
