# Signal-consumption latency hotfix — 2026-09-28

All human-facing observation times below are UTC+8. Scope: active SANDBOX account
only. No risk/strength/TTL changes, forced orders, LIVE credentials, or data deletion.

## Confirmed causes

1. Protection supervision lacked indexes on its business-state subsets. The actual
   SELECT took **18,205 ms** in a read-only EXPLAIN ANALYZE. It rescanned unrelated
   state for 14 settled protection records (70,954 total state rows at measurement).
   Whole protection stages repeatedly took 15–23 seconds. Account protection itself
   was clear; this was database overhead, not a reason to skip safety checks.
2. Expiry discovery repeatedly probed receipts for over 330,000 historical signals.
   An equivalent materialized unreceipted set avoids doing downstream lease,
   decision and intent work for already completed history. Original row locks,
   consumer scoping, statement-time lease discovery and post-lock live lease
   rechecks remain in place. No cursor/high-water mark can lose late commits.
3. A five-minute sample contained 107 S8 decisions rejected only for strength below
   the existing threshold, after assembling market context. A shared pure strength
   predicate can reject these immutable inputs before external context collection.
   This is not a lower threshold or an opening permit. Higher strength still needs
   contract eligibility, market/account context, source rules and all risk gates.

## Changes

- Two narrow partial indexes for registered protection states and recovery keys;
  online build uses `deploy/v2-testnet/protection-lookup-indexes.sql` outside a
  transaction. New installs include the same indexes in the base schema, with an
  idempotent offline migration. Each live index is only 16 kB at initial build.
- Signal-only rejection writes the original signal, frozen configuration, selected
  S3/TV threshold profile and explicit `SIGNAL_ONLY_PREFILTER` audit marker. It
  never invents market context. TV trend overrides are still honored; dynamic
  policy updates are read from the current worker configuration, not a stale
  scheduler closure. Quant-source behavior is unchanged.
- Durable per-scheduler integer-millisecond metrics: discovery, claim, context,
  consume, acknowledgement, elapsed and processed count. No secret/context values
  are added to these metrics. Diagnostic timing uses a separate monotonic clock
  and does not extend the original scheduling or signal deadlines.

## Online index rollout

The initial five-second lock timeout interrupted one concurrent index build.
Inspection proved this newly-created index invalid (`indisvalid=false`); it was
removed and rebuilt, without removing any business data or stopping a service.
Both indexes became valid/ready at **16:58:36**. Subsequent protection-stage time
was **2,198 ms**, versus 15–23 seconds before. Whole cycles initially fell to
18–19 seconds, but lag persisted: index-only success is not full acceptance.

## Verification record

- Before indexes: 3 new regression failures (missing index paths/migration).
- Workspace targeted protection/coverage/pipeline tests: 106 passed.
- Exact deployed-code baseline plus indexes: 106 passed
  (`/tmp/v2-data-qa.SKHsEf/pytest.log`).
- SQL/metrics first integration run exposed a stale test injection hook and a
  float-vs-integer audit-format issue. Both were fixed before deployment.
- Focused rerun for lease renewal, telemetry, pipeline and latency: 48 passed
  (`/tmp/v2-data-qa.2QxbYn/pytest.log`).
- Final exact candidate QA: **468 passed** in 253.59 seconds
  (`/tmp/v2-data-qa.EgwAKH/pytest.log`): prefilter/source thresholds, malformed
  inputs, policy reload, metrics, admission, original scheduling and expiry races,
  idempotency/recovery, full directional rules/replay, pipeline and indexes.
- Additional policy/health/capital QA: **70 passed** in 40.20 seconds
  (`/tmp/v2-data-qa.oAAhQI/pytest.log`). These are isolated mock-venue regressions,
  not a claim to have tested every exchange fault or proved profitability.

## Published runtime and measured tuning

Published `consumer-latency-20260928-v1`, based on the previous exact release.
Four runtime files and three schema/deployment files matched the tested candidate;
no real/fake credential file or unrelated workspace upgrade was copied. Only
the execution controller was restarted. The formal same-account restore advanced
epoch 26 to **27 / ACTIVE / no blockers** with binding unchanged; original native
protection remained intact. Other services retained their existing PIDs.

Metrics then showed that the old **8,000 ms total evaluation budget** still could
not match the observed input rate. At **17:21:52**, the existing `PolicyStore.patch`
API applied exactly `scheduler.evaluation_budget_ms: 8000 -> 16000`, expected
version 4 -> version 5, with account locking and immutable change-reason audit.
All other policy values are unchanged, including 70% capital use, reserved margin,
single-trade/portfolio risk, leverage, TV overrides, signal deadlines and health
confirmation/recovery windows. PREPARED orders still yield immediately for guarded
dispatch; this is extra evaluation time, not permission to chase expired signals.

Observed post-tuning full cycles: approximately **25.9–27.5 s**. S8 processed
**79, 83, 85** signals in recent cycles versus **7–13** in the early index-only
sample. The extra time is useful evaluation work; protection checks remain about
2 seconds instead of 15–23 seconds. The lag finding cleared and the original
120-second recovery window elapsed: at **17:26:20**, health recorded
`active=[]`, `findings={}`. This is a point-in-time acceptance result, not a
guarantee that future bursts or venue outages cannot raise new incidents.

Final bounded observer returned **ACCEPTED**: epoch 27 ACTIVE, no route blockers,
at least three actual complete entry-enabled cycles with protection/exits/settlement
CLEAR, and a fresh empty health snapshot. A further check at **17:30:43** confirmed
health observed at **17:30:31** still had `active=[]`, `findings={}`. This later check
was after the new automatic MUBARAK lifecycle and its income-wait recovery below.

## Actual automatic lifecycle evidence

No manual orders or injected live signals were used for acceptance:

| Contract | Source / strength | Open fill | Close fill | Result recorded |
|---|---|---|---|---|
| QUSDT | S3 / 99 | 16:54:25 | 17:08:13 | SETTLED; outcome at 17:09:24; net -3.33188595 USDT |
| MUBARAKUSDT | S3 / 64 | 17:23:21 | 17:27:02 | SETTLED; outcome at 17:28:10; net -3.24561552 USDT |

QUSDT occurred during diagnosis; MUBARAKUSDT ran after the new release and budget
tuning. HBARUSDT and QNTUSDT also had actual S3-triggered opening fills, both from
strength-99 events. Those observations demonstrate functioning automated data ->
decision -> guarded entry -> exit -> settlement -> analysis paths, not a positive
expectancy or permission to deploy LIVE.

## Safety findings retained, not suppressed

- Some candidates were rejected **before any venue order POST** with
  `CAPITAL_SINGLE_TRADE_RISK_EXCEEDED`. For one MUBARAK candidate, the planning
  reference was 0.06260 and the fresh mark was 0.06401. The final capital gate
  retained its stricter current-notional/risk check. No risk limit was enlarged.
- After the later MUBARAK close, settlement temporarily returned
  `WAITING_INCOME_LAG`, then reached SETTLED automatically. Admission was paused
  during incomplete financial reconciliation; original protection remained.
- Intermittent `INCOMPLETE_ACCOUNT_READ` also conservatively paused admission and
  cleared on subsequent complete coverage. Do not replace these with success or
  hide them by increasing signal TTL/health thresholds.
- Historical scheduler discovery still scales with retained history. The added
  metrics should guide a separate durable-queue capacity design if traffic grows;
  this hotfix does not claim unlimited throughput or remove historical audits.

## Deployment boundaries

Build from `/opt/trade-engine-v2/releases/exit-precision-20260928-v2`, not the dirty
workspace. Runtime changes are limited to `v2_core/scheduling.py`,
`v2_core/directional.py`, `services/v2_directional_admission.py`, and
`services/v2_trading_pipeline.py`. Unreleased quant/UI/account upgrades are excluded.
After a controller-only restart, resume via an explicit same-account owner-API
request with expected epoch/binding guards. Fresh coverage is required before and
after the old worker stops; no direct route-table edits or forced flattening.
