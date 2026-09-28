# Market precision and queue diagnostics — 2026-09-28 (UTC+8)

## Scope and release status

V2 only. No main-branch edit, account switch, trading-policy change, manual exchange
submission/position closure, source-signal replay or database repair was performed.
Diagnostics read PostgreSQL, Redis and ClickHouse. Isolated QA uses separate
disposable PostgreSQL/Redis instances. Only the managed Testnet controller and
shared-market hub were restarted for the coordinated deployment described below.

**Deployed code: controller `505172b`, market hub `364bce9`.** The precision repair
was first released as `364bce9` to both components; the controller subsequently
received the verified expiry-performance patch. The ordinary QNTUSDT
position completed its normal exit and SETTLED accounting at 08:12:07 UTC+8, net
4.60148167 USDT. A signed GET-only Testnet inventory then independently confirmed
zero positions, ordinary orders and conditional orders; the scoped ledger was
clear. This naturally occurring flat interval allowed deployment without closing
anything manually. The market hub uses the immutable release
`/opt/trade-engine-v2/releases/364bce9-market-precision`; the controller now uses
`/opt/trade-engine-v2/releases/505172b-queue-expiry`.

Controller recovery first entered DRAINING. The existing versioned control API
requested the same account/binding; epoch 24 subsequently became ACTIVE, blockers
empty, entries enabled. No SQL route edit or execution-fence bypass was used.
Local QA and historical replay are not acceptance of a complete new-release
trading lifecycle, and the preceding QNT outcome belongs to the previous release.

The subsequent controller-only optimization was also released while independent
Testnet inventory and the ledger were clear. The same control API resumed epoch
25 to ACTIVE with no blockers. Market collection was not restarted a second time.

## Reproduced cause

The frozen legacy feature core rounds price-dimensional ATR/EMA to six decimals.
For VTHOUSDT this erased genuine volatility: archived 1,440-candle frames at
2026-09-28 07:40 and 07:41 UTC+8 produced an hourly ATR of `0.0`, even though the
stored ATR percentage was nonzero. Directional validation required a positive
ATR and raised a generic ValueError; the scheduler returned UNAVAILABLE, blocking
that cycle. This was not a missing balance or an execution-permission problem.

Reconstruction from the same verified ClickHouse candles with the new version
produced ATR values `0.000000214285714286` and `0.000000071428571429` respectively.
The unchanged S8 entry threshold then rejected both signals normally as
`strength_below_minimum`. This does **not** imply either sample should open a trade.
Three SPELLUSDT samples at 07:38–07:40 reproduce the same rounding fault in both
15-minute and hourly ATR; decimal reconstruction restores roughly `0.0000001`.

## Changes

1. Shared market frames now use `s3-shared-v2` / `s3-closed-1m-v2`. Price, extrema,
   EMA and ATR are computed from normalized decimal candles and retained to 18
   decimal places, with explicit rounding. Positive values that cannot be
   represented fail explicitly; no artificial tick/ATR floor is invented.
   Existing EMA seeding and ATR sample selection are unchanged. This is a
   precision repair, not a change to lookback semantics or strategy thresholds.
2. The legacy closed-minute builder defaults to v1, preserving old delivery
   hashes. Shared risk references reconstruct the exact version named by the PG
   frame ID; unknown versions, symbol/time mismatches and digest mismatches fail
   closed. S0 accepts either supported version but rejects mixed-version baskets.
3. Genuinely flat valid candles produce a durable `zero_volatility` rejection,
   not an outage or an opening. Negative/missing/corrupt market values remain
   errors. A regression verifies that this cannot poison another valid symbol in
   the same pipeline cycle.
4. A market-only `score_unreachable` rejection is allowed only if even the maximum
   possible crowding bonus cannot reach the configured minimum. The hypothetical
   bound is labelled as such, not recorded as observed sentiment. Candidates that
   could qualify still need real account, sentiment, funding, sizing and history
   evidence. Source-specific TradingView policy remains in force.
5. Queue health separates non-directional/opposite-direction bookkeeping from
   actionable lag. No receipts/signals are consumed or deleted by this health
   classification. Actionable expired signals, processing errors, unfinished
   decisions and capacity waits retain their existing visibility/semantics.

## Evidence and QA

- First affected-path run: 339 passed, 2 failed. Both failures exposed S0's
  v1-only evidence validator. Fixed the compatibility check and added explicit
  homogeneous/mixed/unknown-version tests before rerunning.
- Final isolated affected-path regression: **408 passed in 161.04 seconds**.
  Includes shared collection, S0, old/new archive verification, directional
  admission/replay, account context, queue health, scheduler latency and pipeline.
- Additional pure score-bound/precision matrix: **138 passed**, including enabled
  TradingView thresholds and zero/maximum crowding bonus configurations. This
  overlaps the affected-path suite and is not an extra independent lifecycle.
- Ruff and diff whitespace checks pass on all changed files.
- Follow-up expiry-performance/lease-race, intent idempotency, pipeline, health
  and admission-latency regression: **260 passed in 226.83 seconds** using a new
  isolated PostgreSQL/Redis QA instance.
- Read-only current-feed sample: 211 signals, 151 non-directional; 7 additionally
  rejected by the safe score upper bound, 6 retained as market candidates. This
  demonstrates avoidable account-query work, not measured live throughput or
  eventual order acceptance. No candidate was submitted by this probe.
- Historical Testnet candles reproduce the precision defect and its correction;
  no expired signal was injected into a formal consumer.

## Deployment checks and remaining acceptance

1. Preserve the current worker's protection/exit management. Wait for ordinary
   flat-account/settled-ledger readiness, or implement and separately verify a
   scoped same-binding maintenance-resume protocol. Never directly set ACTIVE.
2. Release the compatible controller **before** enabling v2 shared-market frames.
   An old controller cannot validate a new frame digest. Publish both components
   from the same reviewed release; keep legacy daemon/watchdog stopped.
3. Allow fresh frames to replace old current-minute projections; never rewrite
   existing Redis/PG same-time evidence to manufacture compatibility. Confirm S0,
   market freshness, archive risk references and account-route readiness.
4. Observe natural signal processing, actual opening acknowledgement/fill,
   leverage/isolation, native protection, exit and settlement after deployment.
   Compare actionable lag and per-cycle latency, not just process liveness.

Initial deployment checks: both systemd units active with zero abnormal restarts;
route ACTIVE; shared market CURRENT and S0 PROJECTED. SPELLUSDT and VTHOUSDT
already expose `s3-closed-1m-v2` envelopes with nonzero ATR. Symbol heads roll to
the new version as their next normal collection runs; old frames remain readable.
The first post-deployment health sample contained one actionable S6 lagging signal,
with non-directional bookkeeping counts reported separately. This was not an
all-clear claim; the persisted recovery confirmation timer still applies.

Further live checks confirmed new-version BTCUSDT/ETHUSDT risk references against
the actual PostgreSQL head and ClickHouse candles. Stale SPELL/VTHO references
were correctly rejected instead of having their timestamps refreshed. New
directional decisions include `score_unreachable`; initial post-deployment
pending-task queries returned no error codes. No new-release opening was yet
observed during these checks, so no new-release fill/lifecycle claim is made.

One overdue AXLUSDT task was left with a five-minute lease during worker handoff.
Its source deadline had already passed; expiry must wait for the lease and never
chase that signal. Do not clear a lease by hand while an owner might still exist.
The health recovery timer requires a sustained quiet interval after cleanup.

A measured cycle spent approximately 7 seconds in historical signal-expiry
maintenance, 4 seconds per directional scheduler and 1.6 seconds in protection.
Read-only EXPLAIN ANALYZE isolated a volatile `clock_timestamp()` lease subquery:
it ran approximately 237,000 correlated lookups, including already-receipted
signals. The follow-up uses a stable statement-start boundary for discovery;
leases expiring during the query are conservatively deferred. The locked final
receipt insertion still rechecks current leases with `clock_timestamp()`, so
concurrently acquired or renewed leases remain protected. Observed read-only
discovery time fell from 4,925 ms to 1,041 ms; this comparison is not a claim about
the full live cycle. No planner settings, queue TTL, lease duration, strategy
threshold or protection check was weakened. A new interleaving test renews a
lease between candidate selection and receipt insertion and verifies no expiry.

After the optimization was deployed, three actual completed cycles measured
expiry maintenance at 2.902, 2.993 and 3.271 seconds, versus the earlier roughly
7-second sample. Full cycle times were 13.807–15.240 seconds. The account remained
ACTIVE and all three cycles were CYCLE_COMPLETE, but health still observed a
short-lived actionable S6 backlog (11 signals in the recorded sample). The lag
alarm was not disabled or declared recovered. Remaining queue throughput / the
30-second processing target needs further work; this release is not an all-green
claim and no new-release exchange fill has been claimed without one occurring.

Residual boundaries: genuine shared-market/dependency faults still block entries;
the pipeline's conservative global failure policy has not been relaxed. The
precision patch does not promise profitability, settle the ATR lookback-model
question, or prove TradingView-origin trading acceptance. The current restart /
same-account resume limitation is not fixed by this patch and remains a separate
operational risk. Disk reserve is monitored by the hub and must not be reduced to
hide storage pressure.

Two stopped QA database copies from this work were compressed and verified before
their uncompressed directories were removed to preserve the market-hub disk
reserve. Diagnostics remain in their original QA roots; recoverable backups are
`/tmp/v2-data-qa.v7q3ir/pg-stopped-20260928.tar.gz` and
`/tmp/v2-data-qa.dB5JIL/pg-stopped-20260928.tar.gz`. No trade database or archived
market history was deleted.
