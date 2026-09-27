# Continuous Testnet admission repair — 2026-09-27 (UTC+8)

## Why the account stopped opening

The previous acceptance cleanup set `V2_CONTROLLER_ENABLE_ENTRIES=false` and
left the controller DRAINING. A completed pipeline cycle did not imply entry
permission. This was an operational shutdown, not evidence of no opportunities.
The user has now authorized repair and ongoing Testnet operation.

The earlier 19:25–21:25 investigation also found 2,793 expired receipts among
4,760 applicable S3 directional-consumer receipts. For the 2,700 pre-scheduling
expiries, signals arrived unexpired with about 55 seconds left on average, but
waited about 70 seconds before their expiry receipts. Never reinterpret raw
source counts or opposite-direction receipts as unique trading opportunities.

## Code changes

- Recover existing decisions first; prioritize fresh supported directional
  events over HIGH_VOL/opposite-direction events in discovery AND task claims.
  Use original deadline order, not a refreshed timestamp. Leases, idempotency,
  durable evidence and expired-signal retirement remain intact.
- Add an account-configurable `scheduler.evaluation_budget_ms` (default 8,000)
  shared across directional consumers. This is a cooperative budget: an already
  running bounded context request finishes before yielding; it is not a hard
  interruption of an in-flight network request or database commit.
- Structural market rejects can terminate before signed account/sentiment reads.
  The actual replay evaluator revalidates the market-only result. Such a context
  cannot authorize sizing/admission. In particular low scores are NOT early
  terminal rejects because verified account sentiment may improve the score.
  Policy changes that invalidate a screen require full account context.
- Yield as soon as an order becomes PREPARED. Existing prepared orders also take
  precedence over further evaluations. Dispatch follows the same recovery,
  protection, exit, settlement and market gates. Immediately recover/protect any
  possibly accepted opening. Historical progress counts run after this critical
  path, never before submission. Maximum one possibly accepted opening per cycle
  remains unchanged.

## TradingView policy semantics

The current Pine trend transition already checks EMA/ADX/DI, while its strength
is a movement/volume score with a floor of 20. It is not the internal S3 strength
scale. Backend evidence does not contain the original ADX value; this release
does NOT invent ADX confirmation, add fake points, or claim calibrated win odds.

`tv.trend_policy_enabled` defaults false for compatibility. When explicitly
enabled for a Testnet account, only TV TREND_UP/TREND_DOWN use
`tv.trend_min_score=20` and `tv.trend_min_strength=20` (the existing short-strength
gate). Other sources/event types retain their existing thresholds. Raw strength,
profile name/version and effective thresholds are frozen with every evaluation.
Sizing validates against the same selected score threshold; leverage still sees
the original resulting score and cannot treat a weak score as a high score.

These are **experimental Testnet thresholds**, not an optimized strategy or a
profitability claim. Contract eligibility, real market confirmation, freshness,
extension/ATR, history, reward/risk, funding, margin and venue checks still apply.
No Pine alert recreation is needed for this backend change.

An offline check used frozen evidence from the preceding eight hours: 75 TV
consumer decisions, 30 with full directional context. Of 15 former short-strength
rejects, six became entry-mode rejects, five overextension rejects, one a score
reject and three reward/risk rejects. None became executable replay candidates.
There were no evaluation errors. This is a counterfactual gate comparison, not
a forward-return backtest. Nothing was reinserted into the signal inbox.

## Operational acceptance requirements

1. Pass isolated PostgreSQL/Redis QA and lint before release.
2. Resolve the current SANDBOX account from the formal route; never switch keys
   or accounts implicitly and never touch LIVE.
3. Verify signed Testnet inventory and local ledger; preserve account capital,
   drawdown and all existing accrued statistics.
4. Update only versioned runtime settings for this account; retain 70% capital
   pool, 0.5% per-trade risk and 5% single-trade margin cap currently configured.
5. Deploy the immutable controller release, enable entries, restart and submit
   an explicit same-account resume request via the existing owner control API.
   A process restart alone deliberately recovers to DRAINING, not OPEN authority.
6. Check actual ACTIVE route, per-cycle entry permission, clear protection,
   signal-consumer reasons/latencies and actual venue/ledger outcomes separately.
7. Leave authorized continuous Testnet operation enabled after observation.
   Do not repeat the previous automatic shutdown at the end of a test window.
   Real safety blockers still revoke admission. No old-signal replay or synthetic
   trade is permitted simply to demonstrate a fill.

## QA and deployed observations

Full isolated V2 regression: **1812 passed in 870.61 seconds**. New targeted
latency/source-policy regressions: **13 passed in 8.79 seconds**. Ruff and
whitespace checks passed. The first test run exposed sizing's old minimum-score
validation after the TV override; sizing now uses the same frozen source policy.

Before release the currently routed Testnet account had wallet/available balance
5,000 USDT, no positions, no ordinary/conditional orders, no inventory blockers
and no unresolved local ledger. Its existing managed base is 1,000 USDT. The
signed GET permission check passed. No account/key switch or LIVE access occurred.

Deployment activation selects signal batch 100 and an 8,000 ms cooperative budget,
with TV-first and the explicit TV trend profile. Original signal TTL, decision
lifetime and all capital/risk limits remain unchanged. Final deployed observations
are recorded separately below. Mocked fills are not real exchange acceptance.
