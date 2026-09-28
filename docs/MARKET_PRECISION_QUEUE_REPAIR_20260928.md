# Market precision and queue diagnostics — 2026-09-28 (UTC+8)

## Scope and release status

V2 only. No main-branch edit, account switch, trading-policy change, exchange
submission, position closure, source-signal replay, database repair or service
restart was performed for this repair. Diagnostics read PostgreSQL, Redis and
ClickHouse. Isolated QA uses separate disposable PostgreSQL/Redis instances.

**Implementation verified, not deployed.** The running execution controller is
still `0c53220`; the market hub is still `8c3ffc8`. An ordinary QNTUSDT position
remains managed with its native stop. Restarting this controller deliberately
recovers into DRAINING; its current reactivation path requires a flat account and
clear ledger. Do not force-close this position or bypass the execution fence just
to deploy. This patch requires a coordinated, safe maintenance window. Local QA
and historical replay are not acceptance of a newly deployed trading lifecycle.

## Reproduced cause

The frozen legacy feature core rounds price-dimensional ATR/EMA to six decimals.
For VTHOUSDT this erased genuine volatility: archived 1,440-candle frames at
2026-09-28 07:40 and 07:41 UTC+8 produced an hourly ATR of `0.0`, even though the
stored ATR percentage was nonzero. Directional validation required a positive
ATR and raised a generic ValueError; the scheduler returned UNAVAILABLE, blocking
that cycle. This was not a missing balance or an execution-permission problem.

Reconstruction from the same verified ClickHouse candles with the new version
produced ATR values `0.000000071428571429` and `0.000000214285714286` respectively.
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
- Read-only current-feed sample: 211 signals, 151 non-directional; 7 additionally
  rejected by the safe score upper bound, 6 retained as market candidates. This
  demonstrates avoidable account-query work, not measured live throughput or
  eventual order acceptance. No candidate was submitted by this probe.
- Historical Testnet candles reproduce the precision defect and its correction;
  no expired signal was injected into a formal consumer.

## Deployment and remaining acceptance

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

Residual boundaries: genuine shared-market/dependency faults still block entries;
the pipeline's conservative global failure policy has not been relaxed. The
precision patch does not promise profitability, settle the ATR lookback-model
question, or prove TradingView-origin trading acceptance. The current restart /
same-account resume limitation is not fixed by this patch and remains a separate
operational risk. Disk reserve is monitored by the hub and must not be reduced to
hide storage pressure.
