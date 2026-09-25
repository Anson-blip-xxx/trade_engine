# Internal multi-account capital journal

This is an isolated-QA foundation, not an activated SaaS or trading balance source.
No exchange calls, credential replacement, exchange transfers, runtime migration,
public endpoint or risk-policy reset is performed by this module.

## Accounting contract

`v2_core/capital_journal.py` binds immutable journal headers and signed entries to
the tenant account registry. Apply `20260925_tenant_registry.sql`, then
`20260925_capital_journal.sql`, once inside a migration transaction. Never apply
these automatically at daemon startup. Existing trade history is untouched.

Every journal has exactly two entries, summing to zero in one explicit currency.
PostgreSQL deferred constraints check the complete transaction, including raw SQL
writes. Exact decimal strings are required; non-finite, zero, out-of-domain and
over-precision amounts are rejected without rounding. Currencies are not converted.

| Event | Cash leg | Counter-book |
| --- | --- | --- |
| Opening / deposit | Positive | External capital |
| Withdrawal | Negative | External capital |
| Realized PnL | Signed | Realized PnL |
| Fee / rebate | Negative / positive | Fees |
| Funding | Signed | Funding |
| Matched transfer | Source negative | Destination cash positive |
| Reversal | Exact opposite of original | Exact opposite of original |

Transfers must remain within the same tenant, exchange, environment and product.
They record an **already matched** movement, not a request to move exchange funds.
Internal transfers eliminate in consolidated reports; SANDBOX and LIVE must be
selected separately. Reversal is append-only, at most once, and cannot reverse
another reversal. Correct a mistake by reversing it and posting a new source event.
Credential rotation preserves account ownership, scope, registry ID and history.

Idempotency is `(tenant, registry, kind, source, source_id)`. Retries with the same
normalized payload return the original journal ID; changed amounts, times, currency
or destination conflict. Distinct accounts may reuse venue event IDs. Tenant-scoped
transaction locks serialize concurrent journal submissions. Database uniqueness,
foreign keys, immutable triggers and deferred balance checks remain a second guard.

## Reports and time

`balances()` returns each currency separately; `consolidated()` requires an explicit
environment. Reporting identities are:

```
net_trading_pnl = realized_pnl - fees_paid + funding_net
book_cash = net_contributed_capital + net_trading_pnl + transfers_net
```

These are historical cash books, **not exchange equity, available margin or opening
budgets**. Unrealized PnL and margin reservations are not represented here. Negative
cash is allowed as accounting evidence, never treated as permission to spend.
Opening balance enrollment must establish an explicit cutoff and must not also
import pre-cutoff cash events. Internal baseline enrollment and projection of
persisted exchange facts are now implemented; see `V2_CAPITAL_INCOME.md`.

Event epoch milliseconds and PostgreSQL ingestion `TIMESTAMPTZ` are both retained.
`as_of_ms` includes events through that instant. UI/export day boundaries must use
Asia/Shanghai (UTC+8), converting to epoch milliseconds before calling this API.
Late-ingested events can restate an event-time report; this is not a frozen or
ingestion-time snapshot. Corrections take effect at their own event time.

## Remaining integration gates

- Verified venue-account identity and explicit enrollment of opening balances.
- Production scheduling/coverage for exchange cash-event ingestion, trade settlement
  linkage, transfer two-leg matching and verified wallet reconciliation. Internal
  replayable fact projection and unverified wallet comparisons are implemented.
- Cash-flow-aware risk anchors; this journal must not bypass current risk controls.
- Authenticated server-derived tenant identity, application-role RLS, scoped public
  APIs and exports. An internal method's tenant UUID is **not authentication**.
- Durable account-switch fencing and two distinct Testnet account acceptance.

QA uses disposable PostgreSQL/Redis only. Tests cover concurrent replay and distinct
events, conflicts, ownership, environment/currency separation, credential rotation,
exact precision, event-time reversals and raw SQL attempts to bypass invariants.

Validation on 2026-09-25: 113 tests passed across capital journal, account registry,
capital model/recovery, multi-cash settlement, directional settlement and Testnet
settlement suites. Ruff lint/format and staged whitespace checks passed. This was
a targeted regression, not a new full-suite run. No runtime migration or service
restart was performed.
