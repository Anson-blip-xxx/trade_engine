# Confirmed capacity declines and consumer fairness

Date: 2026-09-30, all human-readable times UTC+8.

## Observed problem

The Testnet controller on `consumer-capacity-20260930-v2` was ACTIVE.
At 17:57 its most recent five opening attempts were locally cancelled with
`CAPITAL_PORTFOLIO_RISK_EXCEEDED`. The serial pipeline stopped evaluation as
soon as S6 prepared an order, then ended the entire cycle on any DENIED result.
Consequently a valid risk refusal could repeatedly strand the other consumers.
This is a scheduling problem, not evidence that the capital limit is wrong.

The earlier hot-discovery/read-concurrency changes improved some short windows,
but did not establish a sustained latency SLA. A later pre-release ten-minute
direction-matched cohort had S6 390 completions / 105 expired (41.38 s mean,
84.43 s P95), S8 81 / 30 (36.81 s mean, 77.29 s P95). These are completed-only
cohorts, exclude unfinished work, and are not controlled load tests.

At 18:04:56 an AGT opening filled and at 18:06:50 its close filled on the
previous release. The subsequent `WAITING_INCOME_LAG` settlement barrier is
distinct from consumer capacity; the implementation waits at least 60 seconds
after the latest fill before income reconciliation. This trade is NOT evidence
of a fill caused by the new scheduling change.

## Implementation and safety boundary

- Dispatch durable PREPARED orders before additional evaluation.
- Dispatch immediately when a consumer prepares an order. After a proven local
  capacity refusal, continue to the next consumer within the existing deadline.
- A DENIED string alone is insufficient. Query account/environment/product-bound
  order and immutable events: OPEN, version 1 PREPARED -> version 2 CANCELLED,
  no exchange order ID or fills, and the exact atomic-risk reason.
- Only CAPITAL_PORTFOLIO_RISK_EXCEEDED and CAPITAL_SINGLE_TRADE_RISK_EXCEEDED
  qualify. No risk cap, sizing, leverage, TTL or stale-evidence rule changes.
- Unknown submissions, transport exceptions, lost races, other risk failures or
  failed proof reads retain the existing fail-closed behavior. Any potentially
  accepted submission still triggers recovery/protection and ends admissions.
- `scheduler.continue_after_capacity_denial` defaults true (dynamic rollback).
  `scheduler.max_capacity_declines` defaults 4, validated 1..20 and additionally
  bounded by the pipeline batch limit and existing time budget. These are not
  additional parallel order workers. The account lock and atomic risk stay intact.
- The journal records actual DENIED outcomes and exact capacity codes, not fake
  fills. Health exposes them under expected_waits.opening_capacity, including
  observation time. It does not suppress queue lag or other pipeline failures.
- CYCLE_COMPLETE means the bounded orchestration completed, not that a trade
  opened. The entries and capacity_declines fields remain authoritative.

## QA

Initial new-test run exposed three fixture errors (unsupported actor parameter
and missing post-submit cancellation reconciliation evidence). These fixtures
were corrected; production validators were not relaxed.

First isolated PostgreSQL/Redis suite: 157 passed in 151.13 seconds,
diagnostics `/tmp/v2-data-qa.MLJtKS`. Covers scheduling, local-risk classification,
config bounds/rollback, account risk, runtime policy and health. Final expanded
suite and runtime acceptance are recorded below when completed.

Final expanded candidate suite: **346 passed in 360.76 seconds**, diagnostics
`/tmp/v2-data-qa.C71WDL`. Includes the final health-classification change plus
pipeline, health, intent/lease/idempotency, protection, lifecycle, exit and
settlement regressions. Test sets overlap with the earlier 157; do not add them
as a count of unique tests. No live test orders were injected.

New cases cover peer progress, no replay, non-capacity refusals, missing proof,
scope isolation, post-submit cancellation, bounded attempts/deadline, existing
prepared priority, subsequent consumer failure, and health not hiding real lag.
Exchange responses/risk verdicts in these tests are explicit QA injections;
this is not an online exchange acceptance test.

## Remaining acceptance / capacity guidance

### Deployment observation

The three tested runtime modules were installed as
`/opt/trade-engine-v2/releases/consumer-capacity-20260930-v3`, based on v2 (not
the unrelated dirty workspace). Controller PID changed to 1182492; market hub
PID 4157972 did not change. The owner API accepted same-binding resume and the
route was independently verified ACTIVE at epoch 31 with no blockers, 18:14.
One pre-restart S6 task still had a valid lease until 18:18:37.628; it was not
cleared manually. This must be distinguished from new consumer saturation.

Shutdown inspection found a separate follow-up: ChildWorker.stop sets a stop
event, joins 15 seconds, then terminates/kills if needed. The child checks stop
only between entire daemon cycles, while real cycles can exceed 15 seconds.
Thus deployment can interrupt a claimed task and correctly leave it waiting for
the 300-second lease. A future cooperative-drain change should stop new claims
and new admissions while allowing the current claim acknowledgement and any
possibly accepted order recovery/protection to finish. It must be tested with
timeout, crash and account-switch fencing; shortening/clearing leases alone is
not a safe fix. This patch does not change that lifecycle.

At 18:18, nine completed post-release cycles had no ENTRY_BLOCKED result. None
had a new capacity decline, so live continuation after such a decline has not
yet been observed; that branch currently has isolated integration evidence.
The pre-restart LIGHTUSDT task completed naturally as EXPIRED at
18:18:58.436288, after its original lease expired. No leases were manually
cleared. The health sample at 18:18:55 still reported seven S6 lagging signals;
therefore sustained queue-latency acceptance remains open. Route epoch 31 was
still ACTIVE with no blockers.

Post-QA CPU evidence updates the earlier capacity assessment: a ten-second
sample showed average busy 77.2%, idle range 1..48%, average I/O wait 1.3%.
Some earlier short samples reached 95%+ busy. Available RAM was about 5 GiB.
These bursts mean CPU headroom must not be dismissed based on the earlier
40%-idle snapshot. Longer observation correlated with arrival rates, pipeline
timings and database activity is required before sizing extra CPU or a separate
evaluation worker; RAM shortage has not been demonstrated.

Do not describe signal-latency remediation as complete based on this patch.
Evaluation still shares a lifecycle cycle with protection/exits/settlement; a
successful opening intentionally ends admissions. Independent bounded evaluation
with durable pending work needs a separate safety design. Live acceptance must
include sustained arrival/completion rates, age of unfinished work, P95 latency,
expiry ratio, safety-block durations, and no duplicate order/lease violations.

Current short host samples show about 4.9 GiB available RAM and no active swap
traffic, including during QA. This does not prove peak capacity, but does not
justify buying RAM as the first remedy either. Measure steady-state CPU, disk,
database waits and exchange rate-limit headroom after QA stops. Scale CPU/read
capacity only when those metrics demonstrate saturation; never multiply account
order workers to work around a risk or settlement barrier.
