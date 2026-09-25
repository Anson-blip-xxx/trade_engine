# Account opening drain: first trading-switch safety boundary

Internal SANDBOX-only control, not a completed A/B account switch. No key change,
venue call, forced close, automatic reservation release or target activation.
This feature has not been enabled in the running Testnet account.

`AccountDraining.begin(tenant, registry, request_id=...)` validates registry
ownership, locks the account risk scope, and writes a monotonic PG business-state
gate with immutable history. Only the initial request creates the gate; subsequent
requests leave that original evidence unchanged and return fresh local diagnostics.
There is no unhalt/reset API. A tombstone or unexpected payload still blocks.

`Orders.prepare(OPEN)` and `PREPARED -> SUBMITTING` check the same gate while
holding the intent root and account risk locks. The drain writer takes only the
account lock, never an intent lock, preserving lock order. Concurrent submit and
drain therefore have a definite permission winner. No gate means legacy behavior;
no registry migration is needed merely to read the absent gate in the order path.
The control itself requires explicit internal registry enrollment.
Gate reads retain the generic exchange/account/environment/product identity of
existing order records; only the drain-writing control imposes the SANDBOX Binance
futures restriction. This preserves cross-exchange/product recovery isolation.

## Already-issued permits are not cancelled

The committed SUBMITTING transition is the existing runner's submission permit.
Its HTTP request may still be in flight, or even not yet sent, when drain begins.
If that permit won the lock first, this feature does not revoke it or report it
as cancelled. The local diagnostic includes SUBMITTING, UNKNOWN and ACKNOWLEDGED
opening orders; PREPARED orders are shown separately and cannot obtain a new
permit after the gate commits. Existing permits retain their original client ID,
query-only recovery and capital reservations.

Consequently **drain completion is not HTTP quiescence or venue flatness**. The
snapshot always returns venue_flat_verified=false, switch_complete=false and
target_activation_authorized=false, even when its local pending counts are zero.
Local counts are diagnostics, not an inventory certificate, and other ongoing
order/fill updates can change them. Filled positions and external orders still
need venue reconciliation and supervision.

## Exit management and signal handling

Only OPEN paths are gated. Reduce-only CLOSE preparation/submission, protection,
fill ingestion, query recovery and settlement are not disabled by this control.
They still obey all their existing safety checks and must retain account A's
credentials and worker ownership. The feature does not itself launch draining
workers or install missing protection.

New runtime admissions rejected by the gate receive a durable REJECTED result
instead of endlessly retrying an unprepared intent. A prepared order dispatched
after the gate is locally cancelled without calling the exchange. Neither action
rewrites previously submitted/unknown orders.

## Remaining switch acceptance

- Verified venue identity and explicit A/B credential/worker ownership.
- Target B readiness, independent capacity and explicit activation authorization.
- Reconciliation of old permits, venue positions/orders and protection.
- Persistent switch orchestration with A draining while B operates, or a policy
  requiring A flat first; account archival only after all settlement obligations.
- Authenticated/RLS-protected control and real two-account Testnet exercises.

Do not deploy this control as an entire switch workflow or manually delete the
gate to restore entries. Historical gate semantics must survive future migrations.
All execution workers for a gated account must run a version that enforces this
guard before the control is used. An older binary ignores the new state namespace;
rollback to such a binary is not safe while a gate exists. This is an application
permit fence, not a database permission revocation or an exchange-side API lock.

Validation on 2026-09-25: the final complete `tests/v2_core` run passed 1581 tests
in isolated QA. An earlier run found two generic exchange/product recovery-scope
regressions caused by over-restrictive guard reads; those were fixed before the
complete rerun. Ruff lint/format and staged whitespace checks passed. No account
gate was enabled in the running database, no keys changed and no service restarted.
