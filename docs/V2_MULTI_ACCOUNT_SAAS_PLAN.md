# Multi-account capital and tenant isolation

Status: internal registry foundation implemented and QA-tested; not a deployed
multi-tenant capability.
Existing Testnet execution remains unchanged. This plan does not authorize a
LIVE launch, account transfer, forced close, or replacement of current keys.

## Implemented foundation

The preceding Testnet incident-reconciliation gate is now completed: actual
maintenance fills were adopted, locally disposed with symbol quarantine, and
settled through the normal cash/wallet pipeline. This does not activate the SaaS
registry or complete the multi-account acceptance gates below.

`v2_core/account_registry.py` and the additive
`db/migrations/20260925_tenant_registry.sql` provide tenant registration, globally
unique logical account enrollment, UUID-only credential references, versioned
rotation, request idempotency and immutable audit events in one transaction.
Account enrollment always remains ENROLLED_UNVERIFIED and never authorizes
execution. A caller-supplied tenant UUID is not authentication; this internal
module is deliberately not exposed through a public endpoint.

Five isolated PostgreSQL tests cover ownership boundaries, conflicting request
replays, rotation versions, concurrent enrollment replay and concurrent rotation
winners. The registry plus notification/watchdog regression subset passes 18
tests. No registry migration or account/key replacement has been applied to the
running trading database.

Still required: venue identity verification and secret-provider binding,
application-role/RLS isolation, authenticated routing, scoped capital integration,
switch fencing and the two-account end-to-end acceptance gates below. Logical
scope uniqueness alone cannot detect two different IDs for one venue account.

## Verified starting point

AccountScope currently includes exchange/account_id/environment/product. Order
intents, risk reservations, versioned policies and capital state use that scope.
The daemon binds one configured account ID to credentials at composition time.
This logical binding alone is not proof of the underlying exchange account.
Dashboard and performance queries currently aggregate SANDBOX data across
accounts; they are operator views, not tenant authorization boundaries.

## Identity and ownership

- tenant -> globally unique immutable trading-account ID -> deployment/bot.
- Registry owns tenant membership, exchange, venue identity, subaccount/product,
  environment, status, and references to secret versions (never raw secrets in
  business tables, frontend, source control, notifications, or logs).
- Prove venue identity using exchange-supported identity/ownership evidence.
  Where such proof is unavailable, block automated reassignment and require an
  explicit audited enrollment process; wallet amounts or API-key hashes alone
  are NOT account identity. API-key rotation must preserve trading-account ID.
- Multiple keys pointing to the same venue account must share its account risk
  lock/budget. Different bots must not multiply spendable margin.
- Testnet and LIVE remain separate scopes; LIVE activation is a separate gate.
- Existing account IDs are explicitly enrolled under one initial owner tenant.
  Do not rewrite historical order identities or replay external side effects.

## Capital and accounting

- Per-account cash ledger: opening capital, deposits, withdrawals, transfers,
  realized PnL, fees, funding, corrections, reserved margin and reconciliation.
- Balanced journal entries with currency, account ownership, event time, ingestion
  time, venue reference and immutable revision/reversal evidence. Do not overwrite
  old amounts; value non-USDT assets explicitly rather than treating them as zero.
- Deduplicate venue events within account/environment/product/type/venue-event-ID.
  The same external ID in two accounts must not collide; replay within one must.
- Initial allocation, verified balance, trading budget, equity and reserved margin
  are distinct quantities. Each account has its own capital anchor/high water,
  recovery attempts, risk profile and income history.
- Deposits/withdrawals are external cash flows, not strategy profits/losses.
  Define and test cash-flow-adjusted performance/high-water handling; do not
  silently use an anchor reset to erase drawdown or a hard halt.
- A -> B transfer is two reconciled external events linked by transfer reference.
  Owner-wide reporting eliminates internal transfers; account reports retain them.
  Recording a transfer does not grant permission to initiate one at an exchange.
- Final-close trade attribution and event-time daily cash remain UTC+8. Account
  and consolidated views are explicit and must not be mixed or double-counted.

## Switching workflow

Persist a versioned switch operation with source, target, operator, policy,
request ID and per-stage evidence. Duplicate requests resume the same operation.

1. Validate B's tenant ownership, secret/venue identity, permissions, environment,
   balances, positions, open orders and native protection capability.
2. Atomically fence new entries for A at the final submit boundary. Reconcile
   any submission that crossed the boundary before the fence; no timeout release.
3. Put A in DRAINING: protection, exits, reconciliation, settlement and alerts
   continue using A credentials. Do not overwrite them with B credentials.
4. Activate B's independent execution lease and risk/capital policy only after
   readiness checks. A leftovers are never imported as B positions or results.
5. Archive A only after no position/order/unsettled cash, reservations or ambiguous
   outcomes remain. Preserve read-only history and audit trails.

Default is overlap of A management and B new entries, not automatic liquidation.
Provide a stricter wait-until-A-flat mode. If A credentials stop working while
positions remain, alert and block archival; switching B cannot solve that exposure.
If B readiness fails after A is fenced, keep A managed and B entry-disabled; do not
silently re-enable A. Hard-risk halts survive switches, key rotation and restarts.

## SaaS security and runtime

- First deliver one owner with multiple accounts, preserving a tenant boundary.
  Do not publish cross-customer access until authentication/authorization is tested.
- Server-derived tenant identity, account-level roles (read/manage/risk-admin),
  object-level authorization and PG row-level isolation for application roles.
  URLs, query-string account IDs and frontend dropdowns are never authorization.
- Isolate workers, execution leases, Redis keys, jobs, dedup keys, webhook secrets,
  TG destinations, exports and ClickHouse/report access by tenant and account.
- Market data may be shared; private strategy signals and execution receipts may
  not leak across tenants. One signal may route to allowed accounts with separate
  decisions and independent idempotency keys and budgets.
- Exchange IP-wide quotas remain shared in addition to account-specific quotas;
  adding accounts must not multiply the permitted host request rate.
- Start with the current PG/Redis/ClickHouse architecture. No billing system,
  external identity vendor, exchange transfer permission or microservice split is
  required merely to support safe account switching.

## Delivery and QA gates

The internal double-entry capital journal foundation is now implemented; see
`V2_CAPITAL_JOURNAL.md`. It remains isolated-QA only. Milestone 2 is not complete:
exchange ingestion, transfer matching, reconciliation and cash-flow-aware risk
integration still need delivery. Registry and journal do not authorize execution.
The next internal layer now supports immutable baseline cutoffs, atomic projection
of persisted venue PnL/commission/funding facts and unverified wallet comparisons;
see `V2_CAPITAL_INCOME.md`. Runtime worker/coverage and venue-identity acceptance
remain pending; no SaaS migration has been deployed to the trading database.

Attested two-leg transfer accounting is also implemented in isolated QA, retaining
both venue event times and eliminating internal cash transfers on consolidation.
See `V2_CAPITAL_TRANSFERS.md`. Automatic evidence collection, unmatched aging and
full verified reconciliation remain pending; this does not complete milestone 2.

The internal transfer monitor now persists due times, debounced incidents,
reminders/recovery, delivery retry and explicit TG destination binding. See
`V2_CAPITAL_MONITOR.md`. This is QA-tested infrastructure, not an activated
multi-account scheduler or public tenant notification control plane.

An opt-in SANDBOX monitor runner now adds versioned persisted configuration,
account transaction fencing, bounded tenant batches and atomic scan/failure/retry
records. See `V2_CAPITAL_MONITOR_RUNNER.md`. No host scheduler is activated and
this monitoring control does not implement trading-account switch/draining.

The first trading switch boundary is now implemented: a monotonic account opening
drain, serialized with preparation/submission permits and leaving close/recovery
paths available. See `V2_ACCOUNT_DRAINING.md`. Existing permits may still reach the
venue; no target activation, key replacement or completed switch is claimed.

Durable switch preparation now claims both source and target, pins credential
versions, and atomically records explicit source drain with its audit transition.
Cancellation is supported only before draining. See `V2_ACCOUNT_SWITCHES.md`.
Target venue identity/readiness and exclusive runner ownership remain unimplemented
activation gates; preparation claims alone do not block target trading workers.

An encrypted credential vault and unmounted owner account-console candidate now
cover encrypted key+secret enrollment, aliases and selected-account statistics.
See `V2_ENCRYPTED_ACCOUNT_CONSOLE.md`. This does not replace the runtime plaintext
credential file, deploy a key provider, expose a public write API, or enable
execution switching. Those are explicit rollout gates, not completed work.

1. Account/tenant registry and credential-binding guards; additive migration and
   explicit enrollment of existing history, no trading or key replacement.
2. Scoped capital journal, reconciliation and cash-flow-aware performance.
3. Durable switch state machine, fenced account runners and recovery-only draining.
4. Authenticated dashboard account selection, separate/consolidated reports and
   audit-visible controls. Defaults deny unscoped multi-tenant queries.
5. Two isolated Testnet identities: A position open -> B activation -> A exit and
   settlement; repeat switch, crash/restart, concurrent attempts, failed B setup,
   revoked A key, API-key rotation, shared venue identity and ambiguous orders.
6. Negative tenant-access tests for detail IDs, receipts, exports, websocket/event
   streams, webhook replay, TG routing and database application-role bypass.
7. Reconcile deposits, withdrawals, inter-account transfers, partial fills and
   cross-midnight closes. Prove A's losses/high water never alter B's limits.

Mocks verify deterministic boundaries but cannot prove real key-to-venue binding.
Full acceptance requires two distinct Testnet account identities; two API keys
of the same account are a separate rotation/shared-budget test, not an A/B test.
