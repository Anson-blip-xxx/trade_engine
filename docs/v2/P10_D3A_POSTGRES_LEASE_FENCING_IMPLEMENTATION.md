# P10 D3A - PostgreSQL Operation Lease Fencing

> **IMPLEMENTED / CLOSED as dormant persistence logic.** No schema has been
> applied and no recovery worker or runtime caller is wired.

The operation journal now exposes atomic `claim_lease`, `renew_lease`, and
`release_lease` operations. All decisions use PostgreSQL `clock_timestamp()`;
worker wall clocks never decide whether a lease is active or expired.

Each successful lease mutation increments the operation `version`. That
monotonic version is the fencing generation paired with the opaque
`owner_token`. A stage CAS requires the exact owner and version and also checks
that `lease_expires_at > clock_timestamp()`. Therefore an expired worker cannot
advance a business stage even if no replacement worker has claimed yet.

Claim permits only unowned or expired, non-terminal operations. Renew requires
the exact owner, version, and an unexpired non-terminal lease. Release requires
the exact owner, version, and an unexpired lease; it clears owner and expiry and
increments the version. General stage CAS cannot acquire, renew, transfer, or
clear lease ownership.

Typed outcomes distinguish claimed/renewed/released, busy, expired, terminal,
missing, stale-version, owner-mismatch, and write-ambiguous `UNKNOWN`. As with
all journal writes, a lost commit acknowledgement is never reported as a
definite failure.

An opt-in integration suite requires both an explicit test DSN and an explicit
isolated-database confirmation. Against an ephemeral UTF-8 PostgreSQL 16
cluster it validates the real schema, create/lease/stage CAS round trip, and a
two-worker claim race with exactly one winner.

Remaining D3A work is the recovery decision engine, fault-injection and longer
concurrency QA, migration/backup/rollback preparation, and default-off runtime
composition.

**P10 D3A-PG-LEASE-FENCING PASS.**
