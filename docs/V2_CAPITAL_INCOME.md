# Internal cash projection and wallet comparison

Status: internal isolated-QA module. No running service, credential, exchange
position or risk policy is changed. This is not a completed reconciliation engine
or an authenticated SaaS endpoint.

## Data path

Existing `BinanceIncomeImporter` (GET-only transport) -> immutable
`v2_exchange_income` -> `CapitalIncomeProjection.project_window()` -> double-entry
`v2_capital_journals` plus immutable `v2_capital_income_receipts` in one transaction.
Projection reads persisted facts; it does not make an exchange request itself.

Migration order, each applied transactionally once:

1. `20260925_tenant_registry.sql`
2. `20260925_capital_journal.sql`
3. `20260925_capital_income.sql`

None is auto-applied by the trading daemon. Do not apply to a running account
until venue identity, baseline evidence and the rollout gates are satisfied.

## Baseline and attribution

`enroll_baseline()` requires explicit currency, exact opening cash, inclusive
`through_ms`, and a UUID evidence reference. The currency book must be empty.
The immutable baseline and optional nonzero opening journal commit together.
Zero opening cash is supported without a zero-value journal. Exact retries return
the original result; changed cutoff, amount or evidence conflicts.

The caller must retain the actual source evidence behind that reference. The UUID
does not prove a venue balance, account ownership or authoritative cutoff. Baseline
cash must include all events through the cutoff; projection uses only later facts.
An active account snapshot without a known cutoff is not sufficient. A reversed
opening baseline blocks further projection and is reported by wallet checks.

Mapping version `binance-cash-v1` intentionally supports only:

| Venue fact | Capital book |
| --- | --- |
| REALIZED_PNL | Signed realized PnL |
| COMMISSION | Negative fee |
| FUNDING_FEE | Signed funding |

Positive commissions, unsupported types (including transfers and rebates), missing
currency baselines and reversed projected entries remain explicit review items.
Supported zero facts appear in run counts, with no zero-value journal. No transfer
is guessed to be a deposit or silently counted as trading profit. New mappings
require a versioned change and tests, not an unaudited configuration override.

Venue identity includes account/environment/product, income type and transaction
ID. The existing immutable income UUID preserves those components. The projection
uses that UUID as its journal source ID, with a unique receipt and a database
trigger verifying account, timestamp, currency, amount and baseline binding.
These endpoint fields and income types were checked against the
[official Binance income-history definition](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#get-income-history).

This is account cash accounting. Do not additionally post fill-derived/episode
PnL or commission to the same account book: that would count the same cash twice.
Episode allocations and settlement tables are neither modified nor reused as
extra cash legs by this projection. Trade-to-cash explanation remains a separate
reconciliation integration gate.

## Retry, paging and completeness

Each projection page atomically saves new journals, receipts and its audit result.
Failure rolls all three back; successful earlier calls remain. Concurrent calls
share the tenant capital lock. Retries deduplicate by immutable fact, not by a
mutable high-water timestamp. Reversed projected journals are not recreated.

Queries are bounded to seven days and 1–1000 rows per call. `next_cursor` contains
event time plus income UUID, so many events sharing one millisecond still page
correctly. Pass it as `after` with the same window. A truncated page is explicitly
`NEEDS_REVIEW`; it is not complete. Audit rows retain the input and output cursor.
Always rescan overlapping windows from the beginning to discover late facts that
arrived behind a cursor. Do not use the last cursor as a permanent ingestion
watermark. No background scheduler/checkpoint worker is enabled by this module.

`PROJECTED_LOCAL_FACTS` means only that this local page's supported facts were
handled. All results explicitly retain `coverage_status=NOT_PROVEN` and
`execution_authorized=false`. Even a fully fetched exchange page does not establish
complete financial coverage or balance reconciliation.

## Wallet checks

`compare_wallet()` records an explicit caller-supplied asset wallet amount,
event-time cutoff and observation UUID against that currency's book cash:

`difference = supplied_wallet_cash - book_cash_at_cutoff`

The result includes unresolved local facts and a reversed-baseline flag. A zero
difference is `AMOUNT_MATCH_UNVERIFIED`, never a trading permit. This does not
compare available margin, equity including unrealized PnL, or converted multi-asset
totals. Use an asset-level cash observation with a proven matching cutoff.

An observation retry returns the original immutable report even if later facts
arrive. Changed input with the same observation UUID conflicts. Use a new reference
for a new comparison; preserve both reports. Event timestamps remain epoch ms;
UI/report day boundaries use Asia/Shanghai before conversion. No naive date or
UTC+8 offset is written into an epoch timestamp.

## Outstanding acceptance

- Verified venue identity, retained snapshot evidence and trustworthy cutoff capture.
- Worker scheduling, retained/overlapping exchange windows, coverage proofs and
  stale-data detection; retention gaps cannot be repaired by assuming zero income.
- Transfer two-leg matching, other income types, wallet reconciliation and alerts.
- Episode-to-cash linkage and cash-flow-adjusted risk/high-water calculations.
- Authenticated tenant access/RLS and two-account switch/draining acceptance.

Tests exercise the original GET importer with a deterministic fake transport,
then real isolated PostgreSQL facts, cash journals and wallet reports. These are
not new live or two-account exchange acceptance results.

Validation on 2026-09-25: 353 targeted tests passed, covering the new projection,
capital journal/registry, original income importer/admission tests, capital policy
and recovery, directional cash, income ownership and settlement regressions.
Ruff lint/format and staged whitespace checks passed. This is not a fresh full-suite
run. No migration was applied to the running database and no service was restarted.
