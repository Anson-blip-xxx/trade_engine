# V2 adaptive risk and event-time accounting

Scope: isolated Binance Testnet only. No LIVE credentials, no alteration of
existing-position leverage, no reset of historical capital anchors on deploy.

## Recovery

Configured high-water drawdown, not a fixed USDT loss limit. A 10% threshold
corresponds to 100 on a 1000 high-water budget and 200 on 2000. It is an entry
circuit breaker, NOT a guaranteed maximum loss. Existing exposure can lose more.

With recovery enabled: at 6% drawdown PAUSED; after two hours of verified account
observations PROBE at 20% of normal risk (0.1% per trade when normal is 0.5%).
Additional loss of 0.5% of probe-start budget or two losing settled probe trades
fails the attempt. At most two attempts per capital anchor. Exhaustion or 10%
drawdown latches HALTED. Recovery to ACTIVE needs at least three settled probe
trades with positive aggregate PnL AND drawdown below 3%. No midnight/restart
reset, no increase of leverage to recover losses. Missing fresh account data,
unknown orders or missing protection remain independent entry blockers.

State is in the versioned PG capital snapshot. Read-only guards cannot advance
cooldown clocks. HALTED does not self-clear when a timer expires or a floating
loss rebounds. A new capital campaign requires an explicit audited operator
policy/anchor decision after reconciliation; there is no automatic hard-halt reset.

## Adaptive leverage

Normally 2 / 3 / 5; configured boost up to 8. Inputs include signal score/age,
directional 4h/24h trend, taker flow, ATR, funding, drawdown factor, real Testnet
bid/ask prices and available top-of-book size, and account-specific maintenance
margin brackets. 8 requires a fresh TradingView-origin signal, high score and
stop <=4%, alongside all normal confirmation checks. Missing inputs fail closed.
TV is also an independent candidate source; it need not wait for an S3 event.
TV-first scheduler ordering is configurable, with original signal expiry intact.
The bounded market universe and Binance contract validation remain required.

Notional is bounded by capital*risk_fraction/(stop+cost_buffer), independent of
chosen leverage. Higher leverage does not authorize a larger loss budget.
Maintenance screening ignores bracket deductions conservatively, checks both
current and adverse stressed notional, and limits loss+cost+maintenance to 60%
of initial margin. This is NOT a precise liquidation-price guarantee. Gaps,
slippage, funding and changing brackets can still lead to larger losses.
Never move a stop closer merely to qualify for 8x. Frozen leverage evidence is
checked again by the aggregate capital guard; current policy may reject it.

## Reporting

- All calendar grouping uses Asia/Shanghai (UTC+8); timestamps retain instants.
- Daily cash: each owned fill's realized PnL minus fee at execution time, plus
  funding/corrections at their actual event time. Partial closes are separate.
- Completed-trade performance: latest settlement net PnL grouped by the final
  CLOSE fill timestamp, not delayed settlement-processing time.
- These two views overlap and must NEVER be added together.
- Non-USDT fees or missing venue PnL produce unknown daily cash, not zero.
- Headline totals aggregate the whole scoped history, not the first 250 trades.
- Daily mark-to-market is explicitly unavailable without complete boundary
  equity/cash-flow observations; missing historical snapshots are not fabricated.
- Signal funnel counts distinct received / processed / intended / filled signals;
  opposite-strategy receipts do not double the received count. Individual receipt
  reasons are exposed in the read-only dashboard.

## Deployment gates

Run isolated PG/Redis QA, Testnet GET-only bracket/book validation, real-history
report timing, and dashboard-role permission checks. New read-only dashboard
dependencies: v2_signal_receipts and v2_cash_adjustments. Policy changes require
CAS and an operator reason. Keep old release and preserve existing capital
anchor/peak. Rollback must not erase recovery state or use a binary that cannot
parse active policy keys; disabling new admission is safer than legacy rollback.

Testnet execution demonstrates mechanics, not live-market profitability.

## Verification

- Full repository regression: 4343 passed, 10 skipped, one existing legacy
  pytest return-value warning.
- Final affected-chain regression after follow-up changes: 185 passed.
- Additional final-guard positive/negative leverage checks: 13 passed.
- Testnet GET-only bracket and book probe passed; no synthetic order was sent.
- Real-history signal funnel changed from a timed-out correlated query to
  set-based joins; measured approximately 0.2 seconds on the current dataset.
- Ruff, JavaScript syntax and git whitespace checks passed.
