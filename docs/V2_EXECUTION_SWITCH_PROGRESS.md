# Execution switching — not complete

Both Web Testnet bindings passed real signed-read validation on 2026-09-27.
LIVE credentials were not used; legacy Testnet workers remain disabled.

This milestone adds environment-separated, versioned desired-account requests
and status rendering. Request replay is idempotent; concurrent stale requests,
foreign/retired bindings and environment mismatch are rejected. Active workers
cannot be replaced by this endpoint. Web only appends immutable requests with
BLOCKED / execution_authorized=false, enforced by a database CHECK constraint.
It has no write permission to the actual execution route or worker token.

An initial proposal to grant Web writes on actual routing was rejected by the
deployment permission review. The implemented alternative separates requested
state from actual execution state and grants only request INSERT/SELECT plus
non-secret actual-route read access. No activation privilege is granted.

The transaction-held submit guard checks tenant, environment, account, credential
version, epoch, worker token and retirement. It is QA-tested infrastructure,
**not wired into the old deployed trading daemon**. This milestone cannot claim
actual stale-worker fencing or completed exchange-account handover.

Remaining required work: trusted controller and process acknowledgement; source
drain/protection/exits/reconciliation; guard on every signed mutation boundary;
new-account risk/capital bootstrap; isolated LIVE runtime/database/cache and
notifications; real two-account Testnet handover including crash/rollback cases.
All requests currently show EXECUTION_CONTROLLER_NOT_ATTACHED; LIVE also shows
LIVE_DEPLOYMENT_NOT_APPROVED. Saving a request does not launch any trading worker.

Follow-up: the candidate Testnet process composition and executable vault startup
now wire the transaction-held mutation guard; see `V2_MANAGED_EXECUTION_FENCE.md`.
This does not retrofit the disabled legacy deployment or activate a new worker.
