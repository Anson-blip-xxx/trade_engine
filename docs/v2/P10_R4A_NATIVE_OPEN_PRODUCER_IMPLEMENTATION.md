# P10 R4A - Native Active-Open Producer Wiring

> **IMPLEMENTED / CLOSED on isolated V2.** This closes only the first R4
> producer. Migrated, break-even, trailing, and supported legacy lifecycle
> producers remain unwired, so V2 is still not deployable.

## Outcome

The S6/S8 shared-executor open path can now establish canonical protection
identity after a confirmed native fill and enqueue a V3 fenced task. The path is
enabled only when the operator supplies the explicit non-secret
`ACCOUNT_PRINCIPAL_ID`; an absent value preserves the legacy four-field enqueue
shape, which the V2 worker drops.

The producer never derives an episode from `position_id`, symbol, entry price,
or timestamps. It generates a UUID candidate, then uses one Redis Lua operation
to compare the current three-record snapshot and write:

- ACTIVE NATIVE slot authority;
- canonical live-position projection revision 1;
- desired protection generation 1 in `SUBMITTING`, revision 2.

Only the records returned by a successful Redis acknowledgement are used to
construct the V3 task.

## Atomic Handoff

`RedisNativeOpenHandoffAdapter` reads and strictly parses the current authority,
projection, and desired records. Its Lua CAS compares the exact observed values
for all three keys before setting all three proposed records in one script.

Fresh slots require all three keys to be absent. A flat authority may replace
strictly parsed stale projection/desired records from an older generation.
ACTIVE or quarantined authority conflicts fail closed. Same-candidate retries
are idempotent when all acknowledged identity and desired-spec fields match.

Backend read failure returns `UNAVAILABLE`; ambiguous Lua acknowledgement
returns `UNKNOWN`. Neither result enqueues work. There is no file fallback and
no symbol-only fallback.

## Active Boundary

`strategies/shared_executor.open_position` supplies:

- explicit configured principal;
- `DEMO` or `PROD` from the existing Binance testnet selection;
- current ONE_WAY/BOTH slot;
- confirmed fill side, quantity, average entry, and local open timestamp;
- exchange order ID as the open/protection intent alias.

`shared.position_manager._algo_enqueue_native_open` invokes the atomic handoff
and calls `_algo_enqueue` with the ACKed episode, slot generation, protection
generation, desired revision, and projection revision. A non-applied result is
logged and never falls back to legacy enqueue.

## Safety Limits

- Exchange fill occurs before this handoff. Failure therefore leaves an
  exposed position and emits a diagnostic; emergency close policy remains a
  product-gated D2E decision.
- An `UNKNOWN` acknowledgement may have committed the three records without
  enqueueing the in-memory task. Durable handoff/restart replay remains P2.
- ACK of Algo creation still leaves desired status `SUBMITTING`; D2C/D2D must
  verify exchange-active protection.
- Canonical close finalization is not wired yet. Until close transitions the slot
  authority to `FLAT`, same-slot reopen remains blocked and this branch must not
  be deployed.
- Controlled migrated, break-even, trailing, and legacy lifecycle producers
  remain legacy/unwired and are the remaining R4 work.
- This branch was not deployed and did not touch the running main services or
  production Redis.

## QA Contract

Tests cover fresh atomic creation, exact retry, active-episode conflict,
flat-slot reopen, orphan-state rejection, backend failure, ambiguous ACK,
ACK-before-enqueue ordering, failed-handoff no-enqueue behavior, and active
shared-executor routing with no legacy fallback.

**P10 R4A PASS.** R4 remains `IN_PROGRESS` until the remaining producers are
wired and their replacement-generation semantics pass QA.
