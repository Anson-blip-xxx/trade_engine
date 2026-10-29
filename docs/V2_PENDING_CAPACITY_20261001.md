# Pending-signal capacity remediation — 2026-10-01

All human-facing times are UTC+8. Scope is the existing active SANDBOX account;
no LIVE activation, forced orders, historical deletion, TTL extension, risk-cap
change, account switch, or manual lease clearing.

## Diagnosis and evidence

The earlier September 30 releases did not establish sustained latency acceptance.
On October 1, 18:40, execution and market retained their previous PIDs. The host
had about 5 GiB available RAM; short CPU samples ranged from 66% to 98% busy,
with no active swap traffic. Buying RAM is not the indicated first remedy.

The inbox held approximately 937k signals, with 2.25m receipts/tasks. PostgreSQL
estimated hundreds of thousands of unmatched rows although only roughly 100–200
remained. Downstream joins then hashed full historical snapshots and tasks.
A diagnostic pending-ID query took 10.49 s and wrote 79,380 temporary blocks
(roughly 620 MiB at 8 KiB/block). Merely materializing IDs still took 10.02 s:
the planner still expanded subsequent joins into full-history hashes.
Parameterized LATERAL lookups reduced another sample to 2.20 s / 7,788 temporary
blocks. These samples have different pending counts and are not a controlled SLA.

More representative old/new **exact health and expiry SELECTs in one repeatable,
read-only snapshot** returned identical original columns/candidate IDs:

| Query | Previous | Candidate narrow lookup |
|---|---:|---:|
| S6 health aggregate | 3,660 ms | 2,191 ms |
| Expiry candidates (without taking live locks) | 4,041 ms | 2,780 ms |

This showed that narrowing joins alone was insufficient: even narrow historical
reconciliation remained expensive. Recent real phase-history gaps put expiry
maintenance at 5–20 s. Cold discovery sometimes exhausted an entire consumer's
budget before evaluating any signal. Normal close/income reconciliation also
blocked admission; that safety barrier is not a reason to bypass reconciliation.

## Changes

1. Health, cold discovery and expiry reconcile narrow signal IDs first, followed
   by parameterized primary-key reads; avoid rehashing historical wide rows.
   Receipt existence remains authoritative, not task completion alone.
2. Expiry maintenance runs indexed recent-expiry sweeps between full reconciles.
   Existing account-scoped policy `scheduler.discovery_reconcile_seconds` defaults to 60,
   bounded 1–3600. Standalone `expire_pending()` without the option retains its
   previous full-reconcile behavior. Set the policy to 1 for near-every-cycle
   full reconciliation without changing signal expiry or trading thresholds.
   Reusing this existing reconciliation setting avoids introducing a policy key
   unknown to separately deployed Web readers; stored values/digests do not change.
3. Full reconcile runs on startup, clock rollback and interval expiry. A full
   batch immediately leaves full reconciliation due again, so historical backlog
   cannot be parked for the entire interval. No received-at/high-water cursor is
   used. Old late commits and lease releases are recovered by full reconciliation;
   recent expired late commits remain visible in every hot sweep. Old expired
   audit completion can wait until the next full sweep; expired signals never
   become tradable again. Durable inbox/receipts remain in PostgreSQL.
4. Existing row locks, SKIP LOCKED, consumer/account/source boundaries, protected
   decisions/intents, and the post-lock lease recheck remain unchanged. No extra
   order writer or speculative account evidence is introduced.
5. Pipeline actions persist integer `phase_elapsed_ms`, plus per-consumer
   `expiry_metrics` including hot/full mode and full-reconciliation timestamp.
   Health adds `queue_diagnostics` for unreceipted inputs **older than the existing
   lag threshold**: unexpired (`valid_actionable`), expired, leased, and backoff.
   These categories can overlap; unexpired does not mean qualified to open.
   Existing finding counts, confirmation windows and recovery windows are unchanged.
6. Account-cycle ownership explicitly unlocks before returning, on normal or
   exceptional completion. It no longer relies only on asynchronous backend
   cleanup after closing a socket. Failed lock acquisition never unlocks the
   other owner. There remains exactly one account execution owner.

## QA / rollout status

First workspace run: 79 passed, one intermittent protection-order assertion
failed on a second cycle. Expanded candidate run: 287 passed / 308.64 s, diagnostics
`/tmp/v2-data-qa.KWtq2Q`; this did not reproduce that failure. Explicit ownership
release and retained-connection/exception contention tests were added; do not
claim the first failure's cause was conclusively proven by its short traceback.

An initial candidate QA invocation failed at fixture import because the release
does not contain the old `config/binance.env`. Only invalid QA placeholders were
created in the temporary test copy; no real credential was copied. That file must
not be included in deployment.

Historical pre-publication checkpoint (superseded by the final results below):
final expanded QA and runtime observation were pending. Candidate is based on
`consumer-capacity-20260930-v3`, not the dirty
workspace; unrelated quant features are excluded. Only scheduling, pipeline and
health modules are planned for release. Publication must wait for
the final exact-candidate tests.

Subsequent QA before the final configuration-compatibility adjustment: expanded
workspace suite **323 passed / 344.01 s**, diagnostics `/tmp/v2-data-qa.BEMxs4`;
isolated candidate suite **135 passed / 175.85 s**, diagnostics `/tmp/v2-data-qa.t5IIjb`.
These overlap and are not additive counts. During review, a proposed new policy
key was removed in favor of the existing reconciliation setting, avoiding
separately deployed Web-reader incompatibility. The final affected-path candidate
is being rerun after that adjustment with all runtime files frozen.

Exact hot-expiry SELECT (read-only, no row locks or receipt writes) on the live
database: **1.431 ms**, 0 candidate rows, 0 temporary blocks read/written,
134 shared blocks read / 270 hits. This proves the empty hot path is bounded by
the indexed recent range; it is not a burst-load or full-cycle latency guarantee.

Pre-release snapshot at 19:03:35, a five-minute arrival cohort:

| Consumer/source | Arrivals | Completed | Expired | Mean receipt lag | P95 | Unfinished |
|---|---:|---:|---:|---:|---:|---:|
| S6/S3 | 91 | 82 | 4 | 27.90 s | 62.92 s | 9 |
| S8/S3 | 47 | 38 | 8 | 29.83 s | 72.44 s | 9 |
| S6/TV | 1 | 1 | 0 | 34.17 s | 34.17 s | 0 |

Oldest unfinished S8 age was 57.19 s. Latency percentiles above are completed-only;
unfinished counts are reported alongside them to avoid survivorship bias. QA was
running at reduced priority during this snapshot; compare with caution.

## Still outside this remediation

Full historical reconciliation is still O(history), only less frequent and much
narrower. Signal evaluation still shares the lifecycle with protection, exits and
settlement. A separate durable evaluation projection and independently budgeted
read-only work require further fencing, replay, crash and account-switch tests;
do not claim this release introduces them. Safe admission pauses are retained.
More CPU may help sustained resource saturation, but cannot eliminate those
serial dependencies. Acceptance needs ongoing arrival/completion/unfinished-age
and expiration evidence, not just an empty instantaneous alert list.

## Final candidate and deployment

After the compatibility adjustment, frozen exact-candidate affected-path suite:
**107 passed / 120.35 s**, diagnostics `/tmp/v2-data-qa.VMtQRn`. Includes normal
and exceptional explicit lock release with the old connection intentionally held,
other-owner exclusion, periodic expiry/restart/clock rollback/backlog draining,
lease-safe expiry, policy bounds, pipeline protection ordering and health behavior.
Ruff and workspace diff-whitespace checks passed. Earlier expanded suites above
also cover concurrent expiry, lease renewal after candidate selection, stale ACKs,
admission/recovery/idempotency and source/account isolation; counts overlap.

Deployed `/opt/trade-engine-v2/releases/pending-capacity-20261001-v1`, cloned from
the previous online release and updated only these three runtime modules:
`v2_core/scheduling.py`, `services/v2_trading_pipeline.py`,
`services/v2_trading_health.py`. No schema, grant or stored-policy mutation.
The invalid QA-only `config/binance.env` was **not** copied into the deployment.
Old release retained for rollback. Candidate hashes were checked before copying.

Execution controller PID **1574500**, restart count 0. Same-account/binding
resume used the existing owner API with expected epoch and credential binding;
route became **epoch 32 / ACTIVE / no blockers at 19:13:38.449 UTC+8**.
Stored Web policy response remained identical before/after. Market **4157972**,
Web **3765851**, and observer **3565861** PIDs unchanged. No synthetic signal,
test order, forced close, LIVE activation or manual lease clearing was performed.

First new active cycle performed full expiry reconciliation in 3,836 ms; the next
hot cycle spent **47 ms total across four consumers** (11–12 ms per consumer,
including connection/transaction overhead). Cold discovery took 1,420/1,548 ms
for S6/S8 in the first cycle. This is real runtime evidence, not only EXPLAIN.

At 19:14:10, 32 seconds after ACTIVE: S8 had 7 completed post-release inputs,
0 expired, mean 12.36 s and P95 18.09 s; S6 had 7 new unfinished inputs aged at
most 4.34 s. Health had no current lagging rows, but retained the previous active
alert during its original recovery-confirmation window. No timer was shortened.
This tiny initial cohort is **not** sustained latency acceptance; subsequent
observations must be recorded separately rather than extrapolating from it.

## Post-release natural-traffic checkpoint: 19:22:10 UTC+8

Cohort starts at ACTIVE, 19:13:38.449; observation length approximately 8m32s.
Only naturally arriving inputs are included. Receipt completion can mean a
legitimate rejection, not an order. Percentiles include completed receipts only;
unfinished counts and ages are reported separately.

| Consumer/source | Arrivals | Completed | Expired | Mean receipt lag | P95 | Unfinished | Oldest unfinished |
|---|---:|---:|---:|---:|---:|---:|---:|
| S6/S3 | 139 | 134 | 0 | 11.37 s | 19.23 s | 5 | 1.86 s |
| S8/S3 | 149 | 147 | 0 | 11.50 s | 35.93 s | 2 | 6.31 s |
| S6/TV | 1 | 1 | 0 | 28.84 s | 28.84 s | 0 | — |
| S8/TV | 0 | 0 | 0 | — | — | 0 | — |

Route remains epoch 32 / ACTIVE / no blockers. Latest health observation has no
active alerts, findings or over-threshold unreceipted actionable inputs. Original
confirmation/recovery windows and 30-second lag threshold remain unchanged.
Recent hot-maintenance phases were 40–94 ms across all four consumers; one full
reconciliation phase was 3,581 ms. Earlier post-release cold phases reached about
5.4 seconds. Short natural cohorts and different workload/QA conditions do not
justify a controlled percentage-improvement claim.

**S8 P95 still exceeds 30 seconds. Sustained latency acceptance is NOT complete.**
Protection and external context I/O still share the serialized account cycle;
sampled protection phases reached 4.8–6.5 seconds and S6 context phases about
5.5 seconds. A brief TV-context UNAVAILABLE admission pause recovered, but the
exact transient exception was not durably established; do not invent its cause.
The inspected post-release order-update query returned no rows at that checkpoint:
this release does not claim a newly executed open/close end-to-end order test.

Next architectural acceptance items, not implemented in this release:

- Independently bounded read-only signal/context evaluation with fair consumer
  scheduling, while preserving one fenced account execution owner.
- Durable pending-work projection to remove periodic O(history) reconciliation
  from the hot lifecycle without losing late commits or crash recovery.
- Durable per-attempt context-failure evidence and burst/restart/account-switch
  QA; track unfinished age and expiry rate alongside completed-receipt P95/P99.
- Reassess CPU capacity under sustained peak load after these bottlenecks are
  measured. Available RAM was approximately 5 GiB initially; RAM expansion alone
  is not supported as the remedy by this investigation.

No risk limit, capital policy, signal TTL, credential, database schema or LIVE
activation change was used to improve these results.

## Follow-up: fair scheduling and attempt diagnostics

At 21:34:01 UTC+8, the preceding five-minute natural cohort on the previous
release showed S6/S3 35/35 completed, mean 8.16 s / P95 23.18 s, and S8/S3
360/361 completed, mean 6.58 s / P95 11.52 s. Neither directional cohort had
expired receipts; the one unfinished S8 input was 0.07 s old. S8/TV had one
completion at 5.34 s; S6/TV had no arrivals. Health had no actionable lag finding.
Maintenance did expire opposite-direction/non-actionable bookkeeping inputs:
do not equate that count with lost matching-direction entry candidates.

Remaining structural issue: fixed consumer order can repeatedly strand the
later consumer when an earlier context request overruns its cooperative budget.
The follow-up rotates the first consumer within each equal-priority group:

- Existing `scheduler.tv_first=true` retains the TV group before the other
  sources; direction rotates within each group. With TV priority disabled,
  all configured consumers form one rotating group. Existing demand weights,
  total budget, retry cadence and lease durations remain unchanged.
- A group advances once when its first consumer is attempted, not only after
  the whole group finishes. Exceptions, dispatch yields and budget overruns
  therefore do not perpetually pin the same first direction. A BUSY account or
  safety-blocked cycle does not advance the scheduling preference.
- Cursor state is process-local advisory scheduling only, not business truth.
  It resets to configured order on process reconstruction. It writes no local
  file, grants no admission permission, and adds no execution thread. TV-first
  remains priority, not a hard no-starvation guarantee between priority groups.
- Existing PostgreSQL cycle journals now retain `scheduler_order`, per-consumer
  allocation/status/overrun and why an unattempted consumer was skipped.
  `scheduler_diagnostics` retains signal ID, attempt number, failing stage and
  sanitized error code; no context payload or raw exception text is added.
  These records survive subsequent retries clearing a task's current error.
- Failure evidence is retained even when discovery/claim raises before normal
  scheduler return. Partial floating-point timings are rounded before entering
  canonical business evidence, whose numeric contract forbids floats.

This is **not** asynchronous context isolation or a hard cancellation deadline.
An already-running request finishes through the existing evidence/receipt path;
the consumer stops claiming more work when its cooperative budget is exhausted.
The next peer gets the next equal-priority turn, but external I/O may still delay
the current cycle. Full historical anti-entropy is still periodic O(history).
Neither remaining architectural item is marked complete by this follow-up.

Workspace regression before the final two additional tests: **95 passed /
105.09 s**, isolated QA `/tmp/v2-data-qa.6msaVt`. The frozen release candidate
is based on `pending-capacity-20261001-v1`; only pipeline and scheduler modules
are candidates for publication. Unreleased workspace quant support is excluded.
Expanded exact-candidate QA and runtime acceptance are pending at this checkpoint.

Review caught a cross-source identity detail before publication: S3 and TV for
the same direction share `StrategyScope.consumer`. The rotation cursor therefore
uses `(consumer, source)`, not consumer alone. Added multi-round real-pipeline
tests with the shared identity and both values of `scheduler.tv_first`.
The broader pre-final-freeze suite passed **324 tests / 302.78 s**, isolated QA
`/tmp/v2-data-qa.Yak3P7`; because the cursor correction was made during review,
this run is not used as final frozen-artifact acceptance. The entire selected
suite is being rerun on the corrected frozen candidate, including the extra
cross-source cases. Test counts across these runs are not additive.

### Final frozen follow-up QA

Corrected exact-candidate suite: **327 passed / 312.39 s**, isolated QA
`/tmp/v2-data-qa.OZDnAd`. This includes fairness/metrics, pipeline, capacity-denial
scheduling, consumer capacity, bounded pending queries, trading health, intent
admission and execution controller tests. Shared-source cursor identity and
partial-timing serialization were checked before deployment. Ruff, whitespace checks and imports under the
actual managed-controller Python interpreter passed. No external order API was
used by these isolated tests; the QA database was removed by the guarded cleanup
after its own PostgreSQL stopped, with logs retained.

Frozen runtime hashes:

- `services/v2_trading_pipeline.py`:
  `a1d28098daf558f7ab7da3e4ea7285213784ccdcc228c222447b37e2b97be69e`
- `v2_core/scheduling.py`:
  `b8682f70c107c7688b6fe1311f699158268abea87c30ea21b5c10ded8d9c8ff6`

Only these files are allowlisted for `fair-scheduling-20261001-v1`, cloned from
the current `pending-capacity-20261001-v1` deployment. The deployment script
checks hashes, current route/epoch/binding, unchanged stored policy, and unrelated
service PIDs. It uses formal same-binding resume rather than clearing a lease or
fabricating venue readiness. Actual runtime acceptance follows below.

### Follow-up deployment and natural-traffic acceptance

Deployed `fair-scheduling-20261001-v1`. Same account/binding resumed at
**21:53:14.892 UTC+8, epoch 33 / ACTIVE / no blockers**. Controller PID 2184534,
restart count 0 at final inspection. Market, Web and observer PIDs and stored
policy response were unchanged. No synthetic signal, manual order, manual lease
clearing, schema change or credential/account switch was performed.

Consecutive completed cycle journals alternate:

1. S6/TV, S8/TV, S6/S3, S8/S3;
2. S8/TV, S6/TV, S8/S3, S6/S3.

This verifies actual online rotation and preserved TV-first behavior, not just
mock ordering. Allocations and per-attempt diagnostics are present in PostgreSQL.
First cold-cycle S6 exceeded its 7,588 ms allowance by 299 ms, visibly recorded;
the implementation does not pretend cooperative budgeting is a hard timeout.

Natural cohort at **21:59:16.240 UTC+8**, approximately six minutes after ACTIVE:

| Consumer/source | Arrivals | Completed | Expired | Mean receipt lag | P95 | Unfinished | Oldest unfinished |
|---|---:|---:|---:|---:|---:|---:|---:|
| S6/S3 | 96 | 96 | 0 | 11.43 s | 27.64 s | 0 | — |
| S8/S3 | 172 | 165 | 4 | 12.18 s | 21.92 s | 7 | 66.88 s |
| S6/TV | 0 | 0 | 0 | — | — | 0 | — |
| S8/TV | 0 | 0 | 0 | — | — | 0 | — |

All **4/4** S8 expired receipts at this checkpoint were joined back to earlier
`EXISTING_SYMBOL_POSITION` attempt evidence in cycle journals. These are evaluated
inputs waiting on existing-position safety, not evidence of zero consumption.
The generic receipt reason `signal expired before scheduling` alone does not
reveal that distinction. Do not exclude these receipts silently from the table.
Health reported one over-threshold expected existing-position/capacity wait,
no current actionable lag findings, and retained the active lag alert inside its
unchanged recovery-confirmation window. An order-update query for this account
since ACTIVE returned no rows: this is not a new live open/close acceptance test.

### Confirmed remaining restart/lease issue

A pre-release S6 task was received at 21:52:17.786; its first lease was due at
21:57:45.647 while the signal expired at 21:53:30. The previous account cycle
starting 21:52:35.894 remained RUNNING with S6 STARTED. The new controller process
started at 21:52:47. The task had no acknowledgement when inspected afterward.
It completed naturally as EXPIRED at **21:57:51.408**, after the original lease
elapsed, without hand-clearing or re-opening the stale signal. This task predates
the new ACTIVE cohort and is not hidden inside its expiry statistics.

`ChildWorker.stop()` currently waits a fixed 15 seconds before terminate, then
5 seconds before kill; the deployed systemd stop timeout is 45 seconds. There is
no cooperative per-signal stop check in the scheduler. This is consistent with
an in-flight attempt losing its ACK on restart and having to wait out its lease;
the exact interrupted remote request was not captured by the old diagnostics.
The fairness release **does not fix** this restart-drain path. It is a separate
remaining source of real lag and deserves priority over simply adding workers.

Next work should cover cooperative claim/admission stop, finishing or safely
abandoning in-flight reads, and a bounded shutdown grace compatible with systemd.
Crash/restart, account-switch fencing, uncertain submission and late ACK tests
must precede its deployment. Do not shorten leases or manually reclaim active
tasks merely to silence the alert. Independently bounded read evaluation and
durable pending-work projection also remain open. Current short cohorts prove
rotation and diagnostic usefulness, not sustained peak-load P95/P99 acceptance.

Final health check at **22:01:29.789 UTC+8**: active alerts `[]`, current findings
`{}`, expected waits `{}`. The old lease-related alert recovered through its
original confirmation window; no threshold or alert-state mutation was used.

## Cooperative stop follow-up

Further inspection confirmed an additional, more direct restart failure mode:
the deployed unit used `KillMode=control-group`. systemd sends initial SIGTERM
to the whole cgroup, while `child_main` installs no SIGTERM handler. The child
can die before the parent's `ChildWorker.stop()` grace period helps at all.
The installed systemd.kill(5) manual confirms that `mixed` sends initial SIGTERM
only to the main process and retains final SIGKILL for remaining cgroup members.

Two disposable, bounded systemd units with dummy Python parent/child processes
reproduced the difference without databases, credentials or exchange access:
`control-group: CHILD_INTERRUPTED`; `mixed: CHILD_ACK, PARENT_DRAINED`.
Initial journal-based collection found no usable unit log; the successful repeat
used direct `systemd-run --pipe` output. Both dummy services were stopped and
automatically collected, not installed as persistent services.

Implementation:

- The daemon passes its real stop event into the pipeline and scheduler. Already
  stopping calls do not claim work; checks between discovery, claim, context and
  evaluation yield cooperatively. In-flight external I/O is not magically killed.
- A task stopped before business evaluation receives a DRAINED scheduling result,
  not a fabricated decision/receipt. The existing ACK updates only its matching
  lease token, clears that owned lease, and makes unfinished work immediately due.
  A late ACK cannot clear a successor's token. Committed decisions/receipts remain
  authoritative and replay stays idempotent. Failure to ACK retains lease recovery.
- New dispatch is checked again after phase-start journaling. If a request was
  already accepted or its outcome is uncertain, recovery/protection still run;
  stop never converts uncertain submission into cancellation or success.
- Already-started safety stages retain their normal completion path. New market,
  history/progress and notification work is skipped when stop is observed at its
  boundary; durable notifications can be delivered after recovery.
- Child grace becomes `V2_CONTROLLER_STOP_GRACE_SECONDS`, default 30, bounds 1..60.
  Terminate and kill fallbacks each retain a five-second join. A sanitized stop
  outcome reports forced/stopped/grace without account or credential material.
- Unit KillMode becomes mixed and TimeoutStopSec becomes 90, leaving room for
  parent finalization and forced fallback. Production installation is a dedicated
  drop-in, not replacement of unrelated service settings. Whole-cgroup forced
  cleanup remains enabled; old-process confirmation and account fencing remain.

This prevents a normal cooperative shutdown from needlessly abandoning claims;
it does not eliminate lease waits after SIGKILL, host failure, a wedged dependency
or failed database ACK. No lease duration, freshness deadline, trading policy,
secret, schema or cross-account admission rule is weakened.

Initial workspace QA: **104 passed, 2 failed / 90.37 s**, logs
`/tmp/v2-data-qa.qkEgR0`. Both failures were old daemon mock lambdas rejecting the
new `stop_requested` keyword; test doubles were updated, with a separate explicit
callback-wiring test retained. A first expanded invocation had a nonexistent test
filename and ran no tests (`/tmp/v2-data-qa.6gVzij`); it is not counted as coverage.
The corrected frozen four-module candidate is running expanded isolated QA.
Its runtime imports and static checks pass. No candidate has been published at
this checkpoint; final QA and actual lifecycle acceptance must be recorded below.

### Frozen cooperative-stop QA

Corrected exact-candidate suite: **387 passed / 338.99 s**, isolated diagnostics
`/tmp/v2-data-qa.74HOSU`. Coverage includes stop before claim/after read/after
context/after committed decision, immediate retry, successor-token protection,
real spawned-child lease release and resumption, PREPARED retention without
dispatch, accepted submit followed by recovery/protection, bounded force/kill
fallback, daemon stop wiring, actual execution-guard ACLs, managed account
resume/switch, intent idempotency and existing scheduling/expiry behavior.
Static checks, actual-runtime imports and `systemd-analyze verify` pass; the latter
also emitted an unrelated existing tat_agent PIDFile deprecation warning.

Frozen candidate hashes (only these four runtime modules are allowlisted):

- scheduling: `6848a5cc47d9e566b1b95e0b1eb1f95afc303a6bf8db42b67f616aa44af7db4c`
- pipeline: `36b9ea3cf67169812111f5cfa989ac8059360b448bfdf80774a4645b2084e936`
- daemon: `0e3f26d2750b12bdf57a65663ada0d8fa52b888f3c06370c0b6f9f70356d6d2c`
- managed worker: `52b171262525c4dc4275e1fe7e67ed14c93d2f492da36a0742477fa4a551bcd2`

Additional host observation: the first dummy unit could not connect stdout to
the journal socket (`Connection refused`), as recorded in `/var/log/syslog`.
That explains why journal-based experiment collection was unusable; the direct
pipe experiment is the successful signal-routing evidence. No host journald
restart or unrelated logging reconfiguration was performed. PostgreSQL cycle
and lease evidence must be used for deployment verification if stop stdout is
unavailable. This host logging defect remains separate from lease correctness.

Deployment targets `cooperative-drain-20261001-v1` cloned from the previous
fair-scheduling release, plus a new dedicated
`/etc/systemd/system/trade-v2-execution-controller.service.d/40-cooperative-drain.conf`.
The original unit is not overwritten. Immediately before rollout the same
account was epoch 33 / ACTIVE with no blockers and one active strategy lease.

### Cooperative-stop deployment acceptance

Deployed `cooperative-drain-20261001-v1`; same account/binding returned to
**epoch 34 / ACTIVE / no blockers at 22:26:10.420 UTC+8**. Effective systemd values
are `KillMode=mixed`, `TimeoutStopUSec=1min 30s`. Controller PID 2318787,
NRestarts 0. Stored policy and unrelated market/Web/observer PIDs were unchanged.
No synthetic signal, forced order/close, manual lease reset or LIVE action.

The old pre-deployment cycle beginning 22:25:36.473 completed normally. During
same-binding recovery, the new-code child cycle beginning **22:25:53.537** ended
at **22:26:08.266** with `ENTRY_BLOCKED`, `drain_requested=true`, and
`evaluation_stop_reason=STOP_REQUESTED`; its S8 scheduler completed with
`stop_reason=STOP_REQUESTED`. The record is a finished persistent journal, not
an abandoned RUNNING marker. At the immediate post-recovery lease checkpoint
there were **0 active unfinished task leases**, including **0 older than a minute**.

This natural stop happened after an attempt's regular ACK, rather than in the
middle of a successful context read: the live cycle contains no DRAINED result.
Do not claim live coverage of that branch. Mid-context DRAINED/lease release and
successor recovery were verified with a real spawned child against isolated
PostgreSQL in the 387-test suite. No extra live interruption was manufactured to
force that test branch.

At **22:27:43.430 UTC+8**, approximately 93 seconds after ACTIVE, the new natural
cohort had S6/S3 11/11 completed, 0 expired, mean 10.51 s / P95 11.83 s, and
S8/S3 42/44 completed, 0 expired, mean 11.04 s / P95 19.14 s. Two S8 inputs remained
unfinished, oldest 35.50 s; health distinguished an expected existing-position/
capacity wait and reported no current findings or active alert. No TV arrivals
occurred in this initial cohort. This short checkpoint is not peak-load or
sustained-latency acceptance, nor a new open/close trade acceptance test.

Normal coordinated shutdown is now covered. Hard failures may still leave leases;
independently bounded external reads and removal of periodic O(history) work
remain separate next steps. The journal-socket defect also remains a host
observability issue; the present release neither restarts journald nor changes
its global configuration.

Final check **22:29:15.454 UTC+8**: epoch 34 ACTIVE, no blockers, no active health
alerts or findings; no unfinished active lease older than one minute (using the
unchanged configured 300-second lease). Controller remains PID 2318787 with
NRestarts 0.
