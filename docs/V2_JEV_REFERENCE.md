# JEV market reference

## 2026-11-19 independent low-risk sampling mode

An exact SANDBOX account can opt into `V2_JEV_SHADOW_ACCOUNT_ID` through protected
deployment configuration. No account identity or credential belongs in source.
Only that account replaces the JEV admission gate with a non-authoritative
observer; absent or nonmatching configuration retains the existing gate.

The observer freezes the original strategy candidate without synchronous model
or database calls, then attempts a bounded background evaluation using the
existing shared quota. It cannot veto, authorize, resize, defer or extend an
entry. Final account/venue risk checks, native protection and exits still run.
Queue failure, stale evidence and missing results are coverage failures, never
model approval. Separate immutable `jev-shadow-binding-v1` and
`jev-shadow-result-v1` records preserve the source, configuration, original
decision and model clocks. A receipt alone proves neither durable evaluation
nor execution; late research is not retrospectively available at decision time.

Shadow v6 requests distinguish the already-persisted public SANDBOX
`premiumIndex` mark and its exchange timestamp from the original closed 1m
candle price/time and closed 15m indicator time. They never re-clock a candle.
The background worker verifies the immutable account-context reference before
using its public projection; balances, positions and account identity are not
sent to the model. Public marks must be less than 15 seconds old at submission;
the existing shared model freshness limit still applies. The separate candle
research bound is 90 seconds, consistent with the input context contract.
Requests have distinct hashes/prompts, and legacy gate requests remain unchanged.

The reviewed sampling profile changes only new-entry planned stop-risk budgets:
0.25% per trade and 1% aggregate. The 70% margin-pool ceiling, capital anchor,
settlement basis and existing reservations remain unchanged. Planned loss is not
a guarantee against slippage or gaps. The separate CAS tool
`services.v2_sampling_risk_policy` defaults to dry run and requires exact active
route/binding and policy identities to apply; existing positions are not closed
or resized. Deployment/real-input findings and QA boundaries are recorded in
`docs/v2/LOW_RISK_SHADOW_ACCEPTANCE_20261119.json`.

Daily reports classify these entries as `SHADOW_ONLY`, not model approvals or
fallbacks. Existing historical reports keep their original gate interpretation.
Do not promote a model based on raw PASS-versus-fallback PnL: compare prospective
matched opportunities, record missing coverage and costs, and distinguish model
completion time from the original decision time.

## 2026-10-23 TV fusion, availability and effectiveness

This section supersedes the older rule that every JEV availability failure
must skip an otherwise valid candidate. The release remains SANDBOX-only; no
LIVE policy, account binding, balance baseline or trading ledger is reset.

### Decision path and fallback

- Internal scanning and TradingView remain independent candidate sources. No TV
  notification means `ABSENT`, not bearish evidence and not a reason to wait.
- The versioned global `signal-fusion-policy-v1/current` binds only fresh,
  same-symbol, same-environment evidence. A same-direction peer contributes at
  most five soft admission points; a fresh opposite-direction peer vetoes that
  candidate. At equal observation times, conflicting evidence takes priority.
  Duplicates never stack. Original timestamps/expiry and baseline/fused plans
  remain in immutable decision evidence.
- Fusion now runs before the cheap market prefilter. Neither confirmation nor
  JEV raises size/leverage, fixes a bad reward/risk ratio, overrides an unlisted
  contract or weakens account/position safety. Allocation uses the baseline
  score. A decision and its first JEV binding cannot outlive used TV evidence.
- The account `jev-entry-gate-policy-v1/current` adds `unavailable_action`
  (`SKIP` by default, `BASELINE` explicitly enabled for this deployment) and
  `unavailable_wait_ms` (1500). A confirmed operational failure or exhausted
  local request/token allowance may retain the original freshly risk-checked
  decision as `FALLBACK`, with its exact cause and unchanged quantity. A bounded
  unclaimed queue wait may also fall back. A claimed request with uncertain
  outcome cannot be called an outage or silently bypassed.
- A structured non-success provider response is an availability rejection, not
  a bearish model opinion. It is recorded as `PROVIDER_REQUEST_REJECTED`; no
  response text is inspected and no undocumented status is asserted to mean
  insufficient paid credits. Authentication/configuration should still be
  corrected, even when the original deterministic strategy can continue.
  A typed shared-client configuration failure before any provider call is
  separately recorded as `MODEL_CONFIGURATION_UNAVAILABLE`; it is not described
  as a provider rejection or an exhausted paid balance. Arbitrary exceptions
  with similar text cannot select this fallback.
- An actual usable model VETO remains a VETO. Invalid responses, corrupt
  bindings, unknown errors, stale signals, and a response already received but
  too late are not converted into baseline approvals. All real submissions
  still use the original risk and venue guards. Exits and protection never
  depend on TV or JEV.
- Prompt `market-hypotheses-v2` includes available measured funding rate,
  bid/ask spread and validated numeric TV confirmation with original timestamps.
  Missing depth/OI remains unknown. No credentials, account identifier, balances
  or private position data are sent to the model.

### Known cash-publication wait is not unknown exposure

The account `settlement-entry-policy-v1/current` defaults OFF and is explicitly
enabled only after isolated QA. It may separate the known 60-second exchange
income-publication delay from unrelated signal evaluation, but only after two
checks of the same fresh persisted coverage, a complete closed-episode set,
equal real opening/closing fills, terminal orders and still-HELD risk reserves.
Its result is `ENTRY_CLEAR_PENDING_INCOME`, not `SETTLED` and not an order permit.
No profit is credited early and no reservation is released. Unknown cash gaps,
unprotected positions, unowned orders and other blockers retain their behavior.

### Reproducible read-only effectiveness check

`python -m scripts.v2_integration_effectiveness` takes an explicit account,
timezone-qualified window and read-only database connection. It compares JEV
PASS/VETO/FALLBACK, direct/confirming/conflicting/absent TV evidence, and actual
settled PnL separately. Hypothetical markouts use the next full minute's open,
fixed horizons and explicit fee/slippage assumptions. Missing/censored data is
not filled with zero. Matched controls use the same direction, regime, source,
TV usage, version/configuration and UTC+8 date, without reusing pairs. This is
observational research, not causal uplift, a strategy backtest or a promise of
future profit.

The real read-only baseline window 2026-10-21 13:49:19 through 2026-10-23 07:42:36
(UTC+8) contains 759 decisions, 88 JEV PASS, 301 VETO, 155 skipped/unavailable and
215 unreviewed. All 71 actual filled/settled episodes sum to -23.05513875 USDT.
The matched PASS-minus-VETO mean markout is -0.10470471 bps at 15 minutes
(70 pairs), -142.15462302 bps at 60 minutes (70), and +124.48456782 bps at 240
minutes (69), assuming 5 bps fees plus 2 bps slippage per side. Horizon results
disagree; this does **not** establish model benefit. Every 15/60-minute window
is complete; two 240-minute windows were not yet mature at the cutoff.

Exact release hashes, selected isolated regression results, deployment facts
and remaining real-network acceptance limits are in
`deploy/v2-testnet/tv-jev-fusion-acceptance-20261023.json`. Rollback must first
restore compatible versioned policies and then the prior release using the
normal controller drain/resume protocol. Never restore old ledger data over
new trades, reset shared API counters or force a trade to manufacture acceptance.

## 2026-10-21 entry integration (supersedes advisory-only wiring below)

The `simple-entry-20261021` release family moves the model request **after** the
original deterministic strategy/risk admission. It uses the existing scheduler,
PostgreSQL business-state CAS, background JEV queue and guarded order path; it
does not create another order engine or bypass reconciliation.

Flow: fresh signal → existing risk/admission → bounded JEV filter → existing
order preparation/submission → independent TP/SL and exits → settlement/review.

- The dynamic account-scoped `jev-entry-gate-policy-v1/current` defaults OFF.
  When enabled, the global provider control must also be enabled.
- PENDING produces a normal scheduler deferral, with no immutable OPEN decision,
  intent, order or final receipt yet. A bounded same-task continuation can wait
  for the existing PostgreSQL model result, then acquire a **new account context**
  and run the original risk checks again. There is at most one such recheck per
  task claim; otherwise the existing scheduler retry remains. One durable binding
  fixes the snapshot, policy version and deadline across retries and restarts.
- The continuation owns no model client, new queue or order authority. Its poll
  budget is at most the existing model timeout (hard ceiling 2 seconds), also
  bounded by the scheduler's remaining budget and task lease. Connections/SQL
  retain their own bounded timeouts, so this is not a hard real-time wall-clock
  guarantee. It holds no transaction while sleeping, rechecks stop/lease/time
  after the wait and fresh context read, and cannot refresh old timestamps.
- The second account read uses the same candidate-only API budget. Insufficient
  allowance defers the candidate; it never borrows protection/exit capacity or
  reuses the first balance. This improves timely review, not guaranteed entry.
- The review window is fixed at 30 seconds. The deployed reset follow-up uses a
  15-second daemon wait (the prior effective override was 2 seconds).
  Total cycle time and scheduler retry/rotation must still be measured. A missed
  deadline skips this signal; it does not block the account or chase the signal.
- A result is read from PostgreSQL and checked for the exact model, prompt,
  symbol, direction, snapshot and lifetime. PASS cannot change quantity,
  leverage or the original admission decision. The stored decision expiry is
  clamped to JEV/public/signal validity, so a delayed dispatch cannot reuse an
  expired model opinion.
- An enabled gate with missing, failed or vetoed JEV evidence terminally skips
  only this candidate. This differs deliberately from the historical neutral
  advisory-only behavior documented below. OFF remains transparent.
- Exits, protective stops and settlement never wait for or require JEV.
- Existing symbol-opening quarantine is checked before expensive market/model
  work and again at prepare/dispatch. A quarantined symbol is terminally ignored,
  not repeatedly retried as a global infrastructure failure.

The experimental thresholds (`trend_supported >= 0.6`, `chase_risk <= 0.7`) are
configurable filters, **not** calibrated win probabilities. The existing shared
5 requests/minute and 500000 input tokens/day caps remain. Only whitelisted
public market features leave the host; unknown order-book facts remain unknown.

Isolated PostgreSQL tests cover the complete simulated signal/risk/model/order/
native-stop/fill/settlement/outcome path plus expiry/no-POST, VETO and fresh-risk
rejection. These tests do not prove a real network round trip or profitability.

The archive/reset preserves account bindings, aliases, encrypted vault bytes and
policies. Fresh SANDBOX policy baselines require an explicit start-time-zero
marker, an empty ledger and fresh flat venue inventory. Only the reference
wallet, start time and capped initial equity change; LIVE is never reset.

Fresh capital initialization uses the restricted, PostgreSQL-owned
`v2_fresh_capital_scope_clear` predicate from
`db/migrations/20261021_fresh_capital_baseline.sql`. The worker receives EXECUTE,
not SELECT on private capital/account tables. The predicate accepts only the
SANDBOX scope and retains ledger locks through the initialization transaction.
The QA suite must exercise a real non-owner, NOINHERIT worker role; a database
owner-only test cannot validate service permissions.

In the `v2` follow-up, shared-host API budgeting separates candidate reads from
position management. Public collection and admission share the existing
non-critical `market_weight` counter (900/minute default); all lanes still share
the total cap (1800/minute default). Candidate signed reads use a GET-only
transport, and candidate public/sentiment reads use the admission lane too.
Local quota exhaustion defers only the candidate as
`ADMISSION_BUDGET_UNAVAILABLE`; actual exchange errors are not reclassified.
No counters are reset on deployment. Management still has a finite budget,
which must be verified under real cycle frequency and position load. The
shared=false legacy path does not provide this shared-host reserve guarantee.

The initial reset follow-up set SANDBOX's non-critical limit to 350/minute while
retaining the total 1800/minute limit. This reserved 1450 for management; it is
not an unlimited burst guarantee. Under the inspected healthy 10-position
request path, four cycles/minute require about 700 management weight. A first
opening with leverage/margin setup, stop installation and one-page fills needs
up to about 622 additional weight; fill pagination and simultaneous exits can
cost more. These are code-derived capacity estimates, not observed live-trade
results. Only the real endpoint's current limits and runtime evidence govern.

Two redundant read costs are removed without a cross-cycle balance cache:

- `account-inventory-policy-v1/current`, scoped to the account, enables
  `combined_account_config`. The default is false. The separate policy avoids
  adding unknown keys to older Web/observer runtime-policy parsers.
- A fresh `GET /fapi/v1/accountConfig` replaces the two separate mode queries.
  Its two mode fields must be actual booleans; missing/malformed data fails
  closed. The entire real response and digest are preserved; projections name
  their real source. `updateTime` is not used as a freshness clock. See the
  [official account configuration contract](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account#futures-account-configuration).
- The pipeline explicitly passes its current protection diagnostic to exits.
  Reuse requires the exact durable evidence, same scope/exclusions, unchanged
  facts, no blockers, and at most 5 seconds from the original acquisition start.
  Close recovery, invalid evidence or changed facts forces a fresh audit. The
  internal pipeline supplies the current-cycle identity; there is no separately
  persisted cycle-token authorization. Standalone exits retain their old audit.
- Actual order dispatch, emergency reduction and native-stop installation still
  take fresh inventory under their original locks. Diagnostic reuse cannot
  authorize a POST or fabricate a newer observation time. Strict symbol-reduction
  proof validation supports both the legacy and new real-response formats.

There is deliberately no guarantee that all arbitrary concurrent bursts fit a
finite REST budget. Local candidate deferral, expired-signal retirement, actual
management read failures and model expiry must remain distinguishable in QA and
runtime reporting. Never reset counters or loosen evidence freshness to make a
dashboard appear healthy.

### Runtime acceptance findings and follow-up

The first `v2` observation window (2026-10-21 13:12:08–13:17:42 UTC+8) did **not**
pass full entry acceptance, despite 1076 passing isolated regression tests.
Eighteen real six-request account inventories were CLEAR and management had no
local quota failures or exchange 429. However, two timely JEV READY results
expired before the next fresh-risk evaluation, and three TV `STALE_CONTEXT`
failures incorrectly stopped the entire entry cycle. No new orders occurred.

The follow-up addresses the observed paths, not strategy thresholds:

- Same-task JEV continuation above removes the mandatory full daemon sleep
  between a ready opinion and its fresh-risk recheck. Approval lifetime, signal
  expiry, hard risk and final guarded dispatch are unchanged.
- Only `ValueError` with the exact `STALE_CONTEXT` or
  `SIGNAL_EXPIRED_DURING_CONTEXT` code in the **CONTEXT acquisition stage** is
  candidate-local DEFERRED. Other sources may proceed. Missing/malformed data,
  authentication/transport/database failures, wrong-stage errors and failed
  scheduling ACKs are not silently reclassified.
- Background expiry of non-actionable lifecycle/opposite-direction events is
  reported separately from missed actionable candidates. An expired signal is
  never replayed to manufacture an entry.

Release-specific QA, runtime observation and remaining real-trade limitations
belong in the acceptance manifest; simulated full flow is not a network fill.

### Final reset follow-up: admission capacity and explicit input expiry

The `v3` live window (2026-10-21 13:29:35–13:36:01 UTC+8) completed 20 cycles
without a global block, management quota failure or observed exchange 429. A
real JEV response woke the original task after 714 ms, then a fresh account
context and risk recheck rejected that candidate for `JEV_CHASE_RISK_HIGH`.
This proves the real VETO branch, not a new opening/closing round trip. No new
TradingView signal or held-position diagnostic reuse occurred in that window.

The non-critical 350 ceiling still caused 63 candidate deferrals and one
candidate expiry in that window. The `v4` follow-up updated the existing SANDBOX
MarketPolicy (CAS version 2) to total **2000**, non-critical **550**, retaining
the same **1450** management reserve. The versioned change must preserve all
other policy fields and all existing egress counters; no LIVE policy is changed.
A budgeted live `exchangeInfo` observation at 13:42:21 UTC+8 reported a Testnet
REQUEST_WEIGHT limit of 6000/minute. This is a dated venue observation, not a
guarantee for other hosts, concurrent external clients, order-rate limits or
future exchange policy. The existing conservative configuration bounds remain.

If initial JEV request preparation finds an expired public snapshot, `v4`
records `JEV_MARKET_SNAPSHOT_EXPIRED` for that candidate rather than the generic
review-unavailable reason. Fresh account data does not make old market data
fresh. Public TTLs, model deadlines, risk gates and order authority are unchanged.
Tests preserve original timestamps and assert no queue request for expired data.

The `v4` controller started at 13:49:19 UTC+8 and resumed the same SANDBOX
binding through its normal fenced route. A natural IOSTUSDT signal passed the
fresh risk recheck and JEV filter, received a real exchange fill, and installed
a venue-observed `NEW` close-position `STOP_MARKET` using `MARK_PRICE`. The signal
arrived four seconds before v4 startup and was still within its original
expiry at evaluation and dispatch; it was neither injected nor time-refreshed.
This is real entry/protection evidence, not a manual acceptance order. The earlier
prepared order cancelled during route handover had no exchange ID or fill and
must not be counted as another trade. Natural exit, settlement and subsequent
review still require their own real evidence; do not flatten just to produce a
passing acceptance record. Never restore the pre-reset ledger over new orders.

The acceptance manifest records deployment status, exact regression selection,
the corrected test-only PostgreSQL `-infinity` decoding issue, live observations
and the unverified real PASS/order/fill/exit branches. Passing isolated fake-
exchange tests must never be reported as completed real trading acceptance.

## Historical advisory-only implementation

## Boundary

JEV is an optional, asynchronous research reference for S6/S8 entry decisions,
not an order authorizer or an override of account reconciliation, risk, freshness,
position sizing, or exchange filters. Its outputs are uncalibrated hypotheses,
not trade-win probabilities. A model timeout or missing reference is neutral.
No profitability improvement is claimed before an out-of-sample comparison.

The integration uses the shared official client described in
`/home/ubuntu/shared/jev/README.md`: Python SDK `typesafe-sdk==0.7.5`, fixed model
`jev-1.13.0`, fixed prompt version `market-hypotheses-v1`. It does not install a
new web service. The existing shared key stays outside the repository; no key,
account identity, balance, position, signal text, or order is sent to JEV.

## Data and execution flow

1. The existing market-context read supplies its validated public S3 envelope.
2. An in-memory queue accepts only whitelisted numeric public features; it
   performs no additional Redis, PostgreSQL, filesystem, or HTTP read on the
   trading thread. Unknown depth, funding, and open interest stay unknown.
3. One background worker reads the authoritative PostgreSQL control. Disabled
   or invalid control means no model call. The default is disabled.
4. A PostgreSQL coordinator atomically claims the snapshot and reserves the
   shared budget. No network call occurs under its transaction lock. Identical
   ambiguous requests are not automatically retried.
5. The SDK runs in a bounded, disposable child with a sanitized environment and
   zero retries. Only a validated, durably published result may enter the small
   disposable memory cache.
6. The final entry context performs only a non-blocking memory lookup. If the
   exact snapshot/direction result is ready and current, it is frozen under
   `jev_advisory` in the existing decision evidence. Otherwise the context records
   a neutral reason. The trading decision and its hard gates are unchanged.

The reference covers `trend_supported`, `chase_risk`, and `liquidity_fragile`.
The last must not be treated as measured order-book liquidity when depth/spread
inputs are absent. No candle-derived synthetic order book is supplied.

## Bounded operation

- Default SDK subprocess deadline: 1500 ms; maximum configurable value: 2000 ms.
  This is not a promise that all background database work finishes in 2 seconds.
  Background SQL uses a 50 ms lock timeout and 100 ms per-statement timeout;
  establishing its dedicated database connection retains the existing 5-second
  bound. None of these waits are on the trading thread.
- Shared hard ceiling: 5 requests/minute, 500000 input tokens per UTC+8 day.
  A request reserves 8192 tokens. Verified use is charged; unknown use retains
  the reservation. The limits apply across accounts and environments in this
  trading integration, not unrelated projects using the same shared API key.
- Snapshot age: 60 seconds by default; result lifetime: at most 30 seconds.
  Memory control must be less than 5 seconds old. No stale result is reused.
- Disposable queue: 8 waiting jobs, at most 1 executing; cache: 128 entries.
  These are not authoritative business storage. Claims, budgets and results
  live in PostgreSQL. Restarting does not reset the durable budget.
- Frozen trade decision evidence remains authoritative on retries. A new JEV
  opinion cannot rewrite an already stored decision.

## Control and deployment

Control uses `BusinessState` CAS with the key:

```text
exchange=TYPESAFE
account_id=shared-jev-primary
environment=SANDBOX
product=RESEARCH
namespace=jev-advisory-control-v1
key=current
```

The payload is `dataclasses.asdict(AdvisoryPolicy(...))`. Operators must read its
current version and update using CAS, never overwrite trading tables. Setting
`enabled=false` stops new calls when the worker rereads control; a running request
remains bounded by its deadline and local references expire with the control.
Missing, deleted, or invalid configuration degrades to neutral/OFF.

`deploy/v2-testnet/50-jev-reference.conf` keeps unrelated home directories hidden
and exposes only the shared client and existing private configuration read-only.
Both binds are optional, so missing JEV files cannot prevent controller startup.
No service restart alone authorizes OPEN or resumes a DRAINING account.
The child environment omits trading secrets, but the fixed trusted SDK still
runs as the same OS user. This is not a hostile-code sandbox or protection
against a compromised SDK reading other same-user resources. Independent OS
credentials and a public-data IPC worker would be required for that boundary.

Rollback is the previous immutable controller release, with the JEV control
disabled. The JEV drop-in can be removed separately after restoring
`ProtectHome=yes`; do not delete evidence or reset quotas as a rollback action.

## Acceptance and follow-up

Tests cover malformed/stale/poisoned output, strict identity, process deadlines,
credential isolation, parallel claims, global quota, refunds across UTC+8
midnight, queue saturation, unavailable database/model, unchanged admission,
and immutable decision evidence. Isolated QA cannot prove a live order was sent.

An authenticated SDK smoke test proves API access, not model usefulness. Compare
future trade outcomes and matched rejected candidates by model/prompt/strategy
version before allowing any calibrated policy effect. Account reconciliation
incidents remain separate blockers even if this reference is healthy.

JEV coordination namespaces are not yet part of the diagnostic-retention
allowlist. Their growth is bounded by the API quotas but is not zero; add a
separately tested retention policy before increasing those quotas. Never delete
an active claim or current budget window, or trade decision evidence.
