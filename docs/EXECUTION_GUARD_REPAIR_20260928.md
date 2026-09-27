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
- Additional deployed-environment finding: systemd exposes root-owned credentials
  with a service-UID POSIX ACL (mode 0440, directory 0550). The new monitor's first
  private-file-only check incorrectly rejected this legitimate shape. The follow-up
  uses descriptor/O_NOFOLLOW reads, exact existing service-UID ACL validation and
  the secure memory-mount check, not blanket group-read permission. Actual deployed
  credential validation passes; a further 87 targeted tests pass.

## Actual natural-signal Testnet lifecycle

- Episode: `8a5b9295-e1c8-5215-a4f9-3be3574ec14b`.
- Fresh S3 signal: `451df440-6a5d-5483-9339-1053a3d1da78`, RAREUSDT TREND_UP,
  real collected market evidence, accepted by the unchanged S6 admission rules.
- 07:20:22.845 UTC+8: BUY 2,682 at 0.02274, exchange order `185172990`.
- Venue settings independently read back as ISOLATED, leverage 3,
  auto-add-margin false; desired settings state VERIFIED and readiness recorded.
- Native close-position STOP_MARKET `1000000221094193` installed at 0.02092.
- 07:22:24.768 UTC+8: normal reduce-only TIME_STOP exit filled 2,682 at 0.02229,
  exchange order `185173788`. Native protection then expired, no residual position
  or conditional order remained in the independent venue read.
- Settlement revision 2 completed at 07:23:34 UTC+8. Ledger opened/closed quantities
  agree; gross PnL -1.2069 USDT, fees 0.06038523, net -1.26728523. The directional
  outcome/analysis row was written and the ledger reports SETTLED.
- This short-time-stop acceptance proves execution/accounting, **not profitability**.
  It is not a TradingView-origin lifecycle sample. Monitor delivery/pin acceptance
  and restoration of temporary configuration are recorded separately below.

## Additional acceptance and configuration restoration

- A second natural QNTUSDT episode, `ae3f2261-bcfc-5194-8799-56ce001ffcfb`, also
  completed opening, automatic close and SETTLED accounting. Net PnL was
  -0.2130416 USDT, including 0.0230416 USDT fees.
- All four confirmed OPEN/SETTLED TG projections have delivery receipts and
  `telegram-pin-v1` status PINNED. Independent account-scoped health records refresh
  after the systemd ACL fix; the opening-stalled incident clears on new acceptance.
- Temporary time stops were restored to 120/480/240 minutes (S6A/S6B/S8), policy
  version 4. The original no-fixed-count SANDBOX position configuration was restored
  at risk-policy version 7, retaining the 70% capital-pool and other risk controls.
  Restoration and both completed episodes are audited in
  `sandbox-lifecycle-acceptance-v1 / execution-guard-20260928`.

## Expired cross-consumer backlog found by the restored monitor

The monitor exposed old S8 pending LONG signals whose S6 intents incorrectly
prevented S8 expiry. Both `StrategyScheduler.expire_pending()` intent checks had
matched only `signal_id`, not account/product/environment/producer. This was not
evidence of missing S8 short trades. The scoped check preserves each consumer's
own decisions, intents and active leases; other accounts/strategies no longer pin
its expired input. It does not delete source signals or history, change TTL, replay
entries, or suppress genuine lag alerts. A brief flat-account maintenance window
is used for this final queue-correctness deployment.

Final scoped-expiry/admission, health and actual worker-wiring regression:
264 passed in 132.83 seconds using the standard isolated PostgreSQL/Redis harness.

## Final runtime handoff

- Deployed code: `0c53220` (`/opt/trade-engine-v2/execution-controller` immutable
  release link); same SANDBOX account, route epoch 23 ACTIVE, no route blockers,
  entries enabled, controller and shared-market service both active, zero restarts.
- Both S6 and S8 now have zero unreceipted S3 signals received before 07:18 UTC+8.
  Source records and all prior failed-order evidence remain intact.
- After restoration of normal policy, a third natural QNTUSDT OPEN filled
  (`210119723`, episode `92d92214-ceaf-582d-b9f3-c594e9bd81e1`). An independent
  Testnet GET confirmed quantity 0.2 and native close-position stop
  `1000000221108251` NEW at 260.96. This is a normal ongoing position, not an
  unfinished short-time-stop acceptance trade; its existing management continues.
- Since initial deployment: three OPEN fills, two CLOSE fills, one cancelled OPEN,
  no local REJECTED opening. The completed short-time-stop samples remain settled.
- **Not an all-green claim:** the active monitor still reports short-term signal
  lag and intermittent entry-cycle blocks. Recent S8 context attempts for
  SPELLUSDT/VTHOUSDT include `UNAVAILABLE`/`ValueError`; their finer data-validation
  cause has not been established in this repair. The latest protection, exit and
  settlement stages are CLEAR, and the actual live Testnet position has its native
  stop. These observations must not be hidden or described as completed QA.
- No LIVE endpoint/account was used, no strategy entry threshold was weakened,
  and no remote Git push or main-branch change was performed.

Only disposable, stopped QA database copies were removed during this work. Logs
were retained; the older stopped QA cluster `vjckty` was compressed into
`/tmp/v2-data-qa.vjckty/pg-archived-20260928.tar.gz` before removing its duplicate
directory. No trading database, Redis trading state or trade history was removed.
