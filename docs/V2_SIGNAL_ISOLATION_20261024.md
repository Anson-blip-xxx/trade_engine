# Signal consumption isolation — 2026-10-24 (UTC+8)

Status: deployed to the same SANDBOX binding; bounded acceptance passed with
capacity limitations. Activation: 2026-10-24 10:37:27.834 UTC+8, route epoch 7.
Evidence is recorded in
`deploy/v2-testnet/signal-isolation-acceptance-20261024.json`. Do not infer
profitability, zero future latency or a natural closing test from these results.

## Observed cause

The deployed TV/JEV release still promoted a candidate's missing/stale long-short
ratio to a whole-cycle safety failure. In the investigated 24-hour window about
21% of cycles were ENTRY_BLOCKED; most had clear protection, exit and settlement
checks. Candidate validation and public market collection also share a bounded
non-critical host request budget. Cycle completion was followed by an additional
15-second delay. These are control-flow and I/O-budget problems, not evidence of
CPU exhaustion.

## Invariants and changes

- Recognized symbol-data failures are classified by exact cause, stage and
  exception contract. They never grant execution authority and do not poison
  unrelated consumers. Unknown failures, authentication, database integrity,
  account ownership, acknowledgement and shared safety failures remain closed.
- Repeated, validated missing/stale sentiment for one signal has a terminal
  abstention path (default second same-cause attempt). It locks the original
  signal and verifies the exact current task lease and persisted prior cause,
  then atomically stores evidence, an IGNORED/EXPIRED decision and its receipt.
  It cannot call JEV, reserve risk or create an order. Existing decisions and
  intents win unchanged. Wrong-stage or differently typed errors cannot supply
  proof of a prior validated candidate failure.
- Public long-short ratio validation runs before signed account queries. Only
  valid public ratio responses may be cached (bounded size, short TTL, original
  event time, exact environment/symbol scope, deep copies). Private balances,
  positions, margin and leverage are never cached or defaulted. Account checks
  and final source freshness checks still run for every eligible candidate.
- Work time counts toward the configured cycle interval. Cycles never overlap
  or catch up in bursts. Overruns retain a minimum pause, and escaped failures
  retain the original full delay.
- Recent expired inputs are retired on a short, indexed, bounded path. Cold
  historical reconciliation keeps its separate cadence. A cold timeout does
  not cause full-history scans at the fast cadence or get hidden by hot success.
  Live leases, existing decisions and intents retain their recovery ownership.
- Health explains valid backlog, expired bookkeeping, candidate dependencies,
  API-read quota, actual account capacity, and recovery hysteresis separately.
  Thresholds and actual lag findings are not relaxed or deleted.

A missing data feed is not repaired by changing its timestamp or assigning a
neutral value. A signal that cannot be safely evaluated must have an explicit,
auditable wait/abstention/expiry outcome. New signals retain independent
eligibility; no arbitrary symbol cooldown is introduced.

## Release boundary

The candidate is derived from the actual deployed release, not from all dirty
workspace changes. The same changes are merged back into the permanent V2
workspace without discarding unrelated unpublished quant/research work. No
schema, secret, account switch, LIVE execution, capital policy or host request
limit change is part of this release.

The runtime policy schema is unchanged. Actual controller and account-console
readers are compatible. Dashboard and market-hub have older unused copies of the
runtime-policy module; they are not being wholesale upgraded as part of this fix.

## Acceptance and rollback

Use isolated PostgreSQL/Redis tests with external sockets denied. In addition to
focused failure cases, rerun strategy, JEV, queue leases, execution fencing,
protection, exits and settlement regressions. Live acceptance uses naturally
arriving signals, normal controller activation of the same Testnet binding,
measured phase and receipt latency, and exact dependency reasons. Never inject an
order or force a close merely to obtain a passing test.

For rollback, retain the previous release and normal route recovery protocol.
Restore only the execution release pointer after cooperative shutdown, then
resume the same binding through an explicit route request and verified controller
activation. Do not restore an old database or remove receipts, reservations,
positions, API counters or current execution fences.

## Measured acceptance

The candidate passed **1,920 tests across 70 relevant test files** using isolated
PostgreSQL/Redis and a denied external network. The final QA log is
`/tmp/v2-data-qa.RORTM4/pytest.log` (SHA-256 recorded in the acceptance JSON).
New tests cover exact error provenance, lease identity, concurrent admission,
transaction rollback, immutable evidence/receipts, original expiry, zero order
creation by abstention, private-read freshness and actual child-loop cadence.

The first 15 minutes after activation are compared with an equal-length window
before deployment. Received cohorts were re-read after maturation; no pending
tail was silently discarded. Opposite-direction and non-directional bookkeeping
are excluded from the following directional latency figures.

| Metric | Before | After |
| --- | ---: | ---: |
| Completed / globally blocked cycles | 17 / 19 | 59 / 0 |
| S6 non-expiry receipt P95 | 56.939 s | 27.262 s |
| S8 non-expiry receipt P95 | 36.458 s | 18.467 s |
| Same-direction signals received | 787 | 840 |
| Same-direction expired receipts | 19 | 2 |

After maturation, all 840 directional signals in the new window had a receipt:
838 non-expiry outcomes (including one INTENT) and two expirations. Fourteen
repeated stale-sentiment candidates produced atomic, scoped abstention evidence,
matching receipts and no corresponding intent. Two natural TradingView inputs
were handled in 12.284 s and 13.153 s; their decisions were IGNORED for
`entry_mode_unconfirmed` and `overextended`, not lost in the queue.

A natural S3 AGTUSDT opening was FILLED, with a persisted fill, a venue order
identity, an active NEW protective algo and fresh ACCOUNT_COVERAGE_CLEAR.
JEV timed out on that candidate and used the existing auditable baseline
fallback; risk_override remained false. This validates continuity of the
existing fallback, not JEV predictive effectiveness. No order or forced close
was injected for acceptance; a natural closing result is not claimed.

The two expirations are retained explicitly: one AGTUSDT candidate waited on an
already-held symbol; one MOODENGUSDT candidate arrived 63.477 s after observation
with only 26.523 s of original validity remaining. The latter completed expiry
bookkeeping at 32.622 s. Validity was never extended to chase either signal.

## Remaining capacity boundary

This is not a promise of zero latency. The fixed 15-minute window still had
44 API-admission quota deferral occurrences; maximum completed directional
receipt latency was 70.908 s. Short candidate-data lag findings also occurred and
cleared before activating an alarm. The full acceptance JSON includes health
history, pending counts, expired cohorts and those limitations.

Total SANDBOX request capacity remains 2,000/min, its non-critical shared portion
550/min, and management reserve at least 1,450/min. These are API weights, not
capital limits. A larger burst can still wait for the next quota window.
If a tighter burst-latency bound is required, the next capacity work is fair
market/admission allocation plus whole-context reservation with explicit minute
boundaries. It requires coordinated hub/controller QA and must not silently
borrow protection/exit capacity. No such new allocation scheme is claimed in
this release.

The before/after periods had different market workloads. These observations
support this deployment's bounded acceptance, not a randomized capacity study
or profitability claim.
