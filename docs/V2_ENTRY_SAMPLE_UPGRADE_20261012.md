# Testnet entry sample upgrade — 2026-10-12

## Scope and safety

This release adjusts entry sizing and adds account-scoped research evidence. It
does not introduce a new exit strategy, increase leverage/capital ceilings, reset
PnL/equity anchors, or enable LIVE trading. Credentials remain outside source.

The deployment candidate was built from the actually deployed
`drawdown-observation-20261011-v1`, not from the entire working tree. Unpublished
QUANT and legacy structural-gate work has been preserved in the workspace but is
not implicitly included in this release.

## Implemented behavior

1. Remaining-risk sizing: fresh mark and a configurable price-drift envelope
   constrain quantity together with the original fixed stop, cost buffer,
   available margin, per-trade limits, and remaining portfolio stop-risk.
   Quantity is rounded down. Below-minimum orders are rejected, not enlarged.
2. The existing account lock remains authoritative at reservation/submission.
   Original buffered risk stays reserved through partial exits. Concurrent
   candidates cannot each spend the same remaining risk.
3. Structural re-entry uses the last actual filled/settled same-symbol episode,
   not an old OPEN decision or asynchronous observer claim. The canary only
   permits a veto for configured sides and STRUCTURE_CHASE_TOO_FAR. Other WAIT
   and UNKNOWN assessments are observations, not blanket cooldowns.
4. Layered history separates account, environment, strategy, source, side,
   entry mode and regime. A small symbol sample can fall back to its family.
   SHADOW records evidence only; insufficient data cannot trigger adaptive sizing.
   Fees/settlement completeness and both event-time and knowledge-time matter.
5. Controls and proofs are frozen in decision evidence. A configuration change
   invalidates already-prepared opening orders at the final guarded-submit check.
   It does not alter CLOSE submission.

## Controls

The existing versioned BusinessState store uses namespace `entry-controls-v1`,
key `active`, scoped by exchange/account/environment/product. `entry_controls`
provides `read`, `configure(expected_version=..., reason=...)`, and pure frozen
snapshot validation. Changes are audited and accept SANDBOX only.

Current settings (audited controls version 2, 09:01:29 UTC+8):

| Setting | Value |
| --- | --- |
| sizing.remaining_budget_enabled | true |
| sizing.price_drift_fraction | 0.015 |
| history.mode | OFF (performance canary rolled back) |
| history.min_samples | 30 |
| history.prior_weight | 20 |
| reentry.mode | OFF (performance canary rolled back) |
| reentry.enforce_sides | BUY |
| reentry.enforce_reasons | STRUCTURE_CHASE_TOO_FAR only |

Runtime policy remains unchanged: capital pool 70%, per-trade stop-risk 2%,
portfolio stop-risk 3%, per-trade margin 10%. The previously authorized
Testnet drawdown observation mode remains unchanged.

## Latency and causality boundaries

Definitive market-only/universe rejects bypass research/control IO. Eligible
contexts use bounded reads: prior episode SQL 500 ms, archive execution 1 s /
receive 2 s, history SQL 2 s with a 30 s per-provider cache and failure backoff.
These are per-read limits, not proof that total consumption latency improved.
Signal/context expiration is still enforced.

Archive bars have no historical ingestion timestamps. The structure proof records
physical read availability and explicitly sets historical_replay_compatible=false.
It must not be presented as a causal historical backtest. No profitability claim
follows from a replay or from a passing mock-exchange test.

## Regression evidence

A historical mark-drift failure was reproduced without order submission. Original
quantity 10565.3 produced 19.8807588 U stop-risk against an unchanged
19.8222188892 U cap. With the mark/drift envelope the proposed quantity is 8662;
budgeted risk is 19.8221003 U. This is a counterfactual sizing check, not a fill.

Final QA runs (overlapping coverage, not a count of unique cases):

- Core isolated PostgreSQL/Redis regression: 352 passed.
- Full-flow, history, trading pipeline, account risk, controller recovery and
  submission diagnostics: 159 passed.
- Focused full-flow/history/structure regression: 133 passed.
- Workspace merge compatibility (including existing factory variants): 58 passed.
- Changed Python modules pass Ruff; workspace diff passes whitespace checks.

Full-flow tests use real disposable PostgreSQL and an in-memory exchange port;
they do not establish a new Binance fill. All QA databases were stopped and
removed by their bounded runner; diagnostic logs were retained.

The old release's bootstrap SQL omitted the already-deployed immutable coverage
blob table. The candidate bootstrap fixture now includes its existing definition;
no running database migration is needed. Legacy daemon test stubs were updated
to accept the existing stop_requested argument.

## Deployment and rollback

Candidate: `/opt/trade-engine-v2/releases/entry-sample-upgrade-20261012-v1`.
Code activated at 08:54:26 UTC+8 after current-account exchange/ledger checks were flat
and clear, with no unresolved orders and zero initial margin. Only the
execution-controller service/symlink was changed. The same account/binding was
resumed through the route request API, advancing epoch 42 to 43; ACTIVE and no
route blockers were verified. Runtime policy remains version 6; capital base,
peak, anchor and drawdown-control settings are unchanged. Release manifest
contains 16 runtime Python files, including unchanged pure dependencies. Version 1
initially enabled history SHADOW and narrow BUY reentry ENFORCE. Their functional
tests passed, but their live performance canary was not accepted; both were turned
OFF with an audited version 2 control change at 09:01:29, without restarting the
service. Remaining-risk sizing stays enabled.

Natural post-release decisions contain controls version 1, fresh mark,
29.1194124711 U remaining portfolio stop-risk, structural assessments and
SHADOW applied_factor=1. Sampled candidates were rejected by existing score or
reward/risk gates, not by fabricated expiry bypasses. No new completed trade has
yet validated this version against the exchange.

Read-only online observations: history reads 172/289 ms, previous-close lookup
26 ms, 360-minute archive read 877 ms. A preceding history query hit its 2 s
timeout; failure remains bounded and enters the cache backoff. These are point
measurements, not p95/p99 guarantees.

Acceptance caveat: SIGNAL_CONSUMPTION_LAG was observed for 2 then 5 valid S6
source signals after restart, while the health query itself took 5–6 s per source
direction. No new-module task error was observed. By the 08:59:14 UTC+8 check,
findings were empty; at 09:00:02 UTC+8 the hysteresis window also cleared the
active alert. This was only a point-in-time recovery. A longer check found worse
latency: across all sampled S6 consumer decisions (not a direction-filtered
cohort), received-to-decision median/p95 changed from 12.47/24.52 s (179 decisions)
to 25.03/62.06 s (160 decisions). Sampled cycle median changed from 11.43 s
(170 old cycles) to 23.25 s (10 new cycles). This comparison does not isolate
causation from candidate mix, restart warmup, database load or new reads.

The two research switches were therefore disabled as a conservative canary
rollback. The structural proofs themselves took only 16–113 ms in this sample,
with no archive read, so blaming the archive would be unsupported. Research
reactivation requires a direction/source-filtered latency comparison and a new
accepted performance canary. Functional tests alone are insufficient.

The controller remained ACTIVE with no restart during the configuration rollback.
No new exchange fill validated the upgraded execution path in this window.

At 09:02:45, the corrected comparison filtered both `source=s3` and the applicable
directional events, excluding opposite-direction bookkeeping:

| S6 phase | Decisions | Median | P95 | Over 30 s |
| --- | ---: | ---: | ---: | ---: |
| Previous release | 264 | 13.170 s | 27.107 s | 11 |
| Research ON | 188 | 24.077 s | 61.499 s | 45 |
| Research OFF, sizing ON | 36 | 12.835 s | 27.354 s | 0 |

S8's OFF cohort had 10 decisions, P95 25.249 s, none over 30 s. At that check,
direction-applicable pending signals were S6=2 and S8=3, all below the lag
threshold and without task errors. Six OFF cycles through 09:03:01 had median
11.020 s, close to the previous baseline. This short recovery window supports
leaving research OFF, not declaring long-term stability or a single proven cause.
Next work is to decouple/cache research reads and pass another performance canary
before activating either experiment. Do not broaden risk limits to compensate.

Rollback uses a new audited control version with all experiments OFF, followed by
the previous immutable controller release and normal same-binding recovery,
only after current-account exposure is flat and unresolved orders are cleared.
Preserve evidence, orders and balances. A rollback is not a database restoration.
