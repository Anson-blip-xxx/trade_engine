# Execution guard repair — 2026-09-28 (UTC+8)

## Incident and acceptance boundary

The managed SANDBOX worker used the ordinary database connection for its execution
fence, although the controller used an explicit noninherited operator role.
`ExecutionRoutes._binding()` needs SELECT and a column UPDATE privilege for its
`FOR SHARE` lock on `v2_tenant_accounts`. The ordinary worker lacked those rights.
This affected settings, opening, protection and reduce-only mutations, not just
candidate selection. Do not bypass the fence or grant the worker blanket access.

The diagnosed eight-hour interval contained 195 admitted opening attempts:
193 local rejections and two cancellations, zero fills. Two rejected attempts
originated from TradingView. Every local rejection hid the actual permission
failure behind `VENUE_READINESS_BLOCKED`; rejected cycles were marked complete.
Previous isolated bootstrap-owner tests did not reproduce deployed ACLs.

Passing unit/integration tests is not evidence of exchange execution or profitable
trading. Release acceptance requires a fresh natural signal, an actual Testnet
fill, matching leverage/isolation, native protection, a genuine exit, reconciliation
and settlement. No replayed signals, synthetic candidates or LIVE credentials.

## Repair

- A dedicated noninherited `trade_v2_execution_guard` role grants only routing and
  binding inspection/row-lock rights. No vault or credential reference access;
  no route activation/epoch/token update rights. Existing grants are preserved.
- Both managed entry points enter that role transaction-locally. The actual child
  checks its exact binding and real row locks before READY. Every signed mutation
  still reacquires OPEN or MANAGE fencing through its response.
- SQLSTATE 42501 before I/O produces `EXECUTION_GUARD_PERMISSION_DENIED`, including
  startup failures. Safe typed errors survive settings/readiness layers and persist
  with `submission_sent=false`. Ambiguous exchange writes still require recovery.
- Failed dispatches become `ENTRY_BLOCKED`, not a successful trading cycle.
  Recovery, protection and exits continue on subsequent cycles.
- Parent-process health monitoring follows the active account binding, independent
  of the trading child. It reports actual rejection rates and permission faults to
  DM with durable deduplication. Silence or a late old-order observation cannot
  prove recovery; a newer accepted attempt is required.
- The independent maintenance loop also retries pending TG lifecycle-message pins.

## Deployment and rollback safety

1. Apply `deploy/v2-testnet/execution-guard-grants.sql` once, transactionally, as the
   deployment administrator. Validate the actual ubuntu session enters the guard
   role and can hold OPEN/MANAGE fences without making exchange requests.
2. Run isolated core QA, plus restricted-role and actual child-wiring regressions.
3. Publish an immutable local release; restart only the managed Testnet controller.
   Leave shared market collection and all LIVE services untouched.
4. Restart initially recovers in DRAINING. Resume the same verified account through
   the existing versioned control API only after venue and ledger readiness checks.
   Never directly set a route ACTIVE in SQL.
5. Accept on fresh natural signals. A temporary, PG-versioned Testnet time-stop of
   two minutes and one-position cap can bound the lifecycle observation. Preserve
   all entry thresholds, capital fractions and protected-exit market checks.
   Restore only the temporarily changed keys with CAS; retain the audit history.
6. Leave normal Testnet entries enabled after acceptance. A regression involving
   uncertain exposure needs reconciliation/management, not a blind old-release
   rollback into ACTIVE. Do not re-enable the legacy daemon/watchdog.

## Verification log

- Deployed minimal-role no-I/O probe: OPEN and MANAGE both pass as
  `current_user=trade_v2_execution_guard`, `session_user=ubuntu`.
- Targeted settings/readiness/guard/notification regression: 71 passed.
- Restricted-role/route/controller regression: 57 passed before final additional
  wiring and startup diagnostic tests.
- Full isolated core suite: 1,886 passed in 996.40 seconds.
- Final serial regression of all affected execution, health, startup, settings,
  notification and policy paths: 229 passed in 136.43 seconds. This includes
  incremental startup/recovery tests added while the broad suite was running.
- Deployed lifecycle results: pending; this document does not yet claim a real
  successful opening/exit or profitable trading.
