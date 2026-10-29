# Web risk recovery — 2026-10-10

## Delivered behavior

Account management → account overview → 风控状态与受控恢复.
GET shows the selected account's capital high water, base, drawdown, mode,
eligibility checks and the latest ten manual authorizations. POST requires an
authenticated owner, exact same HTTPS Origin, account-management action header,
explicit reason, confirmation and an idempotency UUID. Tenant and actor come
from the server, never the submitted body. All displayed times use UTC+8.

This is **one additional Testnet PROBE**, not a reset or a new capital campaign.
It preserves equity anchor, initial equity, reference wallet, peak, cumulative
loss, policy version and exhausted automatic attempt count. Existing engine
logic therefore returns to HALTED on another failed probe, without another
automatic retry. The retained high-water drawdown limit still applies.
There is no guarantee that a loss threshold bounds actual realized losses.

Eligible accounts must be the current ACTIVE Binance Futures SANDBOX executor,
with unchanged credential binding, no queued switch, exhausted automatic
attempts, HALTED mode and drawdown below the configured hard limit. Recovery,
capital model and portfolio protection must remain enabled. The configured
probe factor must be positive and at most 20% of normal risk, not 20% of margin.

Freshness gates: capital observation within 60 seconds and still inside its
original validity deadline; policy must predate the observation; successful
coverage and trading-health checks within 120 seconds. The latter must have
only CAPITAL_DRAWDOWN_HALT active. Initial margin must be zero, funds available,
and all local episodes, unstarted intents, orders and reservations resolved.
Hard-drawdown halts without exhausted attempts are deliberately not supported.
No forced closes, cancelled orders or reconciliations are performed by this UI.

The configured cooldown applies after the last pause or manual authorization.
Current configuration: 2 hours, 20% normal risk, two losing settlements or
0.5% further probe-base loss cause failure. At least three settled probe trades,
positive aggregate net PnL and drawdown below 3% are required to return ACTIVE.
No changing of leverage, 70% capital-pool limit, strategy or signal gates.

## Concurrency / audit

Additive migration: `db/migrations/20261010_risk_recovery_console.sql`.
Append-only `v2_risk_recovery_events` stores request, authenticated actor/reason,
before/after capital health and result. State changes also enter the existing
immutable versioned state history. No generic state write permissions are added
to the web role; only the narrow SECURITY DEFINER functions are executable.
PUBLIC execution is revoked and the function search path is pinned.

Apply serializes by request, execution route and the existing account protocol
lock, then row-locks the capital snapshot. It rechecks the current route,
binding, policy version, anchor and exact recovery state against the preview,
and independently reruns all eligibility checks. Preview expires after five
minutes. Regular capital observations may advance without invalidating an
otherwise unchanged halt, but fresh financial checks are always repeated.
The existing capital publisher uses version CAS, preventing an older writer
from overwriting a successful manual recovery. Network retries use the exact
same UUID/document; duplicate clicks do not grant extra attempts.

After authorization the existing engine may consider new signals; the button
does not create orders and does not clear other safety blocks. Its status is
PROBE_AUTHORIZED, not a promise that an order will fill. Account switching
invalidates in-flight UI previews and prevents late responses from overwriting
the new selected account. Rendering uses textContent, never alias/reason HTML.

## QA and rollout evidence

- 75 related tests passed in isolated PostgreSQL/Redis QA, including HTTP→real
  database authorization, CSRF/authentication, roles, idempotency, concurrent
  duplicate requests, stale/changed previews, account/tenant/environment
  isolation, unfinished business, published snapshot continuity, automatic
  failure back to HALTED and no policy or order mutations.
  Diagnostics: `/tmp/v2-data-qa.tjQfeA`; temporary QA database removed by runner.
- Node race tests cover account switches, double-click, uncertain response
  retry, confirmation and unsafe aliases.
- Actual Chromium headless-shell rendering passed at 390px and 1440px with
  local mock APIs, confirming the panel loads with no horizontal page overflow.
  These browser tests do not invoke a trading endpoint.
- Ruff and JS syntax checks passed. Earlier full Chromium launcher timed out
  on host desktop-service initialization; rendering was rerun successfully with
  the installed headless-shell binary, not reported as a pass on timeout.
- Candidate based on the prior deployed account-admin release, with only seven
  service/UI files overlaid; unrelated workspace changes were not shipped.
- New release: `/opt/trade-engine-v2/releases/risk-recovery-account-admin-20261010-v1`.
  Prior release: `/opt/trade-engine-v2/releases/storage-dedup-account-admin-20261004-v1`.
- Account-admin restarted at 2026-10-10 16:07:41 UTC+8. Trading controller and
  market-hub start times remained Oct 8 16:56:25 and 10:04:42 respectively.
- Post-deployment authenticated loopback HTML, JS asset and risk API returned
  200; unauthenticated GET and missing-CSRF POST returned 403. Actual web DB
  role has no generic state UPDATE privilege.
- Read-only production-state acceptance: eligible=true, blockers=[], HALTED,
  manual recovery event count=0. No actual account was resumed or test order
  submitted for acceptance. The owner must make the explicit UI confirmation.

Rollback: restore the account-admin symlink to the prior release and restart
only account-admin. Keep the additive schema/audit history. Rolling back the UI
does not revoke an already-authorized probe; never imply otherwise. Do not
reset risk state, rewind snapshots or remove audit rows as part of rollback.
