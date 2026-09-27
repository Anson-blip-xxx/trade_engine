# Web verification: least-privilege shared Testnet quota

## Boundary

The account-admin process uses `WebVerificationPermit`, not a per-account
`PublicRateBudget`. All Web account verifications charge the existing Testnet
host bucket shared with the market hub and execution controller. No new account
selection, trading authority, LIVE activation or credentials are introduced.

The Web database role receives EXECUTE on just two SECURITY DEFINER functions:

| Function | Allowed effect |
| --- | --- |
| `v2_web_testnet_verify_permit()` | Consume a fixed 30 units for the fixed Testnet position-mode GET |
| `v2_web_testnet_quota_cooldown(integer)` | Extend the same host cooldown by 1–259200 seconds; never shorten it |

Neither function accepts a host, environment, account, weight, limit, lane or
clock from Web. The permit charges both total and noncritical market counters,
preserving the management reserve. Only database time can roll a minute window.
No caller-facing quota reset exists. Web has no direct privileges on either
`v2_egress_budgets` or the former `v2_public_market_budgets` table.

Functions are owned by the existing non-superuser schema owner, revoke PUBLIC
execution, fix their search path and bind table schemas at installation. Policy
lookup uses the canonical SANDBOX StateKey primary key and verifies its scope.
Quota defaults and numeric bounds match `MarketPolicy`; configured changes are
read on every attempt. Invalid or disabled quota policy fails closed.

The adapter only permits `GET /fapi/v1/positionSide/dual`. It commits quota in a
separate transaction before network I/O, so a failed verification or rolled-back
receipt cannot refund the quota. HTTP 429/418 extends the shared host cooldown
even if the response is not JSON. Existing owner authorization, pinned binding
version, per-account verification lock, cache, encrypted credentials and Testnet
gate remain in force. Verification never authorizes execution.

These are application and SQL privilege boundaries, not a claim that database
functions can make autonomous commits. A caller-controlled SQL transaction can
roll back its own uncommitted function call. The trusted adapter must commit
before sending the external request, as tested. Protect the Web service identity
and encrypted credential access independently.

## Deployment and rollback

1. Apply `db/migrations/20260930_web_verification_quota.sql` as schema owner in
   the existing `trade_v2_testnet` database, with `trade_v2` as current schema.
2. Prepare an immutable account-admin release and retain its old symlink target.
3. Stop only account-admin briefly; apply
   `deploy/v2-testnet/web-verification-quota-grants.sql` in one transaction.
4. Switch account-admin to the tested release and start it. Do not restart the
   market hub, controller or dashboard. Keep entry-disable settings unchanged.
5. Verify effective privileges, Testnet signed-read acceptance through the Web
   API, account list, process health and unchanged trading route.

For rollback, stop account-admin and preserve the new restrictive grants. Do not
restore the old independent quota-table write privileges. A previous release can
serve account management, but old-code verification will fail closed without
those writes; to restore verification, deploy a corrected release retaining the
new adapter. Never reset counters or shorten a live cooldown during rollback.

## QA

`tests/v2_core/test_v2_web_verification_quota.py` runs on isolated PostgreSQL with
a restricted NOLOGIN caller role. It covers ACLs, temporary-table/search-path
shadowing, concurrent Web/market consumption, reserved management capacity,
dynamic and disabled policy, fixed cost/host, LIVE bucket isolation, database
clock rollover, cooldown monotonicity, invalid cooldown parameters, outer
transaction rollback, unsupported routes, denial before network I/O, and
malformed HTTP 429/418 responses. Existing verification/HTTP/credential, shared
market, frontend, rotation and no-file-data-core regressions are also run.

2026-09-27 isolated targeted regression: **141 passed in 62.02 seconds**. Ruff,
`git diff --check` and import under the deployed account-admin Python environment
also passed. This is a targeted regression, not a repeat of the entire V2 suite.

## Recorded deployment acceptance — 2026-09-27, UTC+8

Account-admin now points to `/opt/trade-engine-v2/releases/63f9302-web-quota`.
Only account-admin was stopped/started; the market hub and execution-controller
PIDs remained unchanged. All three services were active with zero automatic
restarts at the final check. Dashboard still points to its earlier release.

Effective PostgreSQL privileges confirmed:

- Web can execute both quota functions, but PUBLIC cannot.
- Neither budget table grants Web SELECT, INSERT, UPDATE, DELETE or TRUNCATE.
- Web cannot INSERT, UPDATE or DELETE the versioned policy table.
- Both functions are owned by `ubuntu`, a non-superuser, with their fixed safe
  search path. No new privileged database role was provisioned.

At approximately 18:36 UTC+8, both existing SANDBOX accounts returned uncached
`SIGNED_READ_ACCEPTED` from the deployed Web verification endpoint. Each returned
`execution_authorized=false`; immediate repeat calls hit the verification cache.
No LIVE account verification was requested. Unauthenticated account-list access
returned HTTP 403; authenticated account-list access returned HTTP 200.

Trading authorization was deliberately not changed: route epoch 16 remained
`DRAINING / RECOVERING_SOURCE`, and `V2_CONTROLLER_ENABLE_ENTRIES=false` remained
in force. This acceptance proves the quota and signed-read integration, not
opening execution, LIVE readiness, trading profitability or multi-day stability.
