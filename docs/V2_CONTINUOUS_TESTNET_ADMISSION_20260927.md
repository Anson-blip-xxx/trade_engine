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

### Initial online observation (through 22:32:29 UTC+8)

Controller release `d5e5d04-continuous-admission` was activated through the owner
API at route epoch 19. Account runtime policy advanced from version 1 to 2, with
only the six documented TV/scheduler values changed. Entries, native protection
and reduce-only exits were all enabled. Controller, market-hub and account-admin
were active with zero automatic restarts. Main/LIVE were not changed.

There were 21 entry-enabled CYCLE_COMPLETE cycles averaging 13,759 ms, compared
with an earlier sample of roughly 18–29 seconds. One entry-enabled and one
recovery-only cycle were blocked by a transient XPIN task UNAVAILABLE result.
That task completed after three attempts with `strength_below_minimum`; no
unfinished task errors remained. Its original diagnostic was not retained in
the task's final mutable error field, so the underlying transient cause is not
proven. This is not counted as a perfectly clean run.

Among post-release applicable S3 receipts, 408 were evaluated/ignored and 16
expired before scheduling; 55 were still pending at this snapshot. Thus expiry
was 16/424 completed applicable receipts (~3.8%), compared with the earlier
58.7% window. Different short market windows and pending arrivals mean this is
an observation, not a controlled throughput benchmark or a zero-expiry claim.
348 decisions used the market-only rejection path.

The genuine 22:30 TV batch was received and consumed:

- QNTUSDT: 13.81 seconds to applicable-consumer receipt; frozen profile
  `tv-trend-v1`, original strength 29, minimum score 20; rejected at
  `REWARD_RISK_TOO_LOW`, not the old source-strength gate.
- ZESTUSDT: 3.80 seconds to `SYMBOL_NOT_TRADING`; no Testnet contract was invented.
- Opposite-direction consumers ignored both normally.

No new orders/fills occurred. Consequently venue entry, native-stop installation,
closing, settlement and trading notifications were NOT newly qualified online.
No claim about profitability follows from this release.

### Cadence follow-up

Remaining expiry revealed that each 13–15 second cycle still slept another ten
seconds. The current Testnet controller now overrides `V2_INTERVAL_SECONDS=2` in
its own protected environment file; the legacy daemon environment is untouched.
The account ledger was verified clear before this restart, and the same-account
formal resume reached epoch 20 ACTIVE. The existing global/market API budgets,
8-second cooperative evaluation budget and all risk controls remain enforced.
The 22:34 budget sample was 447 total / 172 market weight with no active backoff.

At **22:40:27 UTC+8**, the post-cadence cohort had 18 completed entry-enabled
cycles (average 13,460 ms) and one in-flight cycle; all completed cycles were
CYCLE_COMPLETE. There were 228 applicable evaluated/ignored receipts, 32 pending,
zero EXPIRED receipts and no unfinished task errors in that short window. Sampled
minute weights stayed within the existing budget, with no active backoff. There
were still no natural new orders. This is a short stability observation, not a
claim that arbitrary future bursts cannot expire or that the strategy is profitable.
The final route remains epoch 20 ACTIVE, with entries/protection/exits enabled;
the controller is active with zero automatic restarts. It was NOT shut down at
handoff. The acceptance record reached version 3 and retains both observations.

The PostgreSQL acceptance record is `testnet-acceptance-v1` /
`continuous-admission-20260927`, scoped to the resolved runtime account. It
retains pre-change policy, QA results, observed outcomes and the explicit intent
to leave Testnet entries enabled. After future process restarts, the controller
still deliberately enters recovery; never bypass its same-account readiness
checks by editing route rows or assuming process existence means entry authority.
