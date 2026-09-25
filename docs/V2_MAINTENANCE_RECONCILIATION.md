# Maintenance adoption and local reconciliation

## Semantics

`RECONCILED` is a local, audited disposition of a previously UNKNOWN, unbound
reduce-only CLOSE request. It is **not** evidence of exchange rejection,
cancellation or execution. The old UNKNOWN event remains immutable; the new
event explicitly records `venue_outcome=UNKNOWN` and the resolution digest.
Only Sandbox close requests with complete maintenance evidence qualify.

The target symbol is persistently quarantined for new opening orders. There is
no timeout-based release or automatic unquarantine. Preparation and the final
submit transition both check the quarantine. New signals for that symbol become
durable REJECTED intents, rather than retrying indefinitely. Other symbols still
require all normal account risk, freshness, margin and protection checks.

## Evidence gates

1. The approved maintenance case must be bound to an existing CLOSE request in
   the same account/episode. Plan, receipt, quantity, side and venue identities
   must agree. Sandbox/LIVE and account boundaries cannot be substituted.
2. Re-fetch the actual maintenance order and complete fills over GET-only
   transport. Atomically adopt the already-executed order, fees and fills. The
   imported order can never be submitted again, including after restart.
3. Stop the trading runner and acquire the account advisory lock. Require a flat
   target, no ordinary/conditional orders, and an unbound UNKNOWN original
   request still returning order-not-found. Order-not-found alone is insufficient.
4. Compare the entire bounded symbol trade window against durable episode fills:
   trade ID, order ID, symbol, side, position side, quantity, price, fee, currency
   and timestamp must match exactly. Missing/extra/duplicate trades, a full page
   requiring pagination, or an old window block disposition.
5. Recheck flatness; install the symbol quarantine, save the evidence certificate
   and advance the local request to RECONCILED in one transaction. Mismatched
   ledger revisions or missing adoption/quarantine evidence roll back.
6. Cash/funding and wallet reconciliation remain separate. Do not mark the
   episode SETTLED or release its reservation merely because disposition passed.
   The ordinary settlement pipeline must still prove complete economics.

Maintenance exits are labeled explicitly in Telegram and outcome evidence. Their
real profits, losses and fees remain in financial reporting and risk accounting;
they are excluded from automatic strategy learning samples.

## Deployment

Apply `db/migrations/20260925_maintenance_resolution.sql` transactionally while
the trading runner is stopped, then run the matching code release. The internal
operator entry point is `services.v2_testnet_maintenance_reconcile`; it requires
an account ID, approved case UUID and `--apply-approved-resolution`, refuses a
running daemon, and has no exchange write authority. Secrets are read only from
the existing credentials provider and never printed or stored in business rows.

After a RECONCILED row exists, do not roll back to a binary that does not understand
that state. Stop execution and use a compatible forward fix; do not downgrade or
erase evidence to fit an old schema. The additive internal tenant registry from
the SaaS work is unrelated and is not activated by this migration.

## Verified Testnet rollout

Runtime release: `c3c05e457a3f922768126e672828e15d25baef26`.
The full V2 regression passed 1458 tests; a subsequent 86-test selection covered
the final exact numeric serialization and durable quarantine-rejection changes
alongside maintenance adoption/resolution, notification and outcome regressions.

The approved incident passed actual GET-only adoption and disposition. The
normal settlement pipeline then reconciled fills, commissions, funding and
wallet movement, marked the episode SETTLED and released its risk reservation.
The maintenance outcome is marked ineligible for automatic strategy learning.
The real closing Telegram notification was delivered and confirmed PINNED.
Three subsequent pipeline cycles completed with protection, exits and settlement
CLEAR; the latest health observation had no active alerts. All three services
were active and the dashboard returned HTTP 200.

This restores the account-level pipeline, not permission to trade the quarantined
symbol or activate LIVE. New orders still depend on normal strategy/risk checks.
The SaaS roadmap remains separate and is not claimed complete by this incident fix.
