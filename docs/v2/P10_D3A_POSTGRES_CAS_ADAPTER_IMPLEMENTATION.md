# P10 D3A - PostgreSQL Operation Journal CAS Adapter

> **IMPLEMENTED / CLOSED as a dormant injected adapter.** No connection is
> configured, the standalone schema is not applied, and no runtime caller is
> wired.

`PostgresOperationJournal` implements three narrow persistence operations:

- create one `NEW`, version-1 immutable operation intent;
- read one operation by `operation_id`;
- advance exactly one legal state transition with SQL CAS on
  `(operation_id, version, owner_token)`.

The adapter accepts a transaction-scoped `connection_factory`; it does not
import `psycopg`, read environment variables, or reuse the current optional
ledger helper. This keeps activation and credentials outside the domain and
prevents an import from opening a database connection.

## Result semantics

- create: `CREATED`, `ALREADY_EXISTS`, `CONFLICT`, `UNKNOWN`;
- read: `FOUND`, `NOT_FOUND`, `UNAVAILABLE`;
- CAS: `APPLIED`, `NOT_FOUND`, `STALE_VERSION`, `OWNER_MISMATCH`, `UNKNOWN`.

Every write exception is `UNKNOWN`, including a lost commit acknowledgement:
the caller must read/reconcile and must not assume that the write failed.
Read-only failures are `UNAVAILABLE`. A zero-row CAS is classified inside the
same transaction by reading the current version and owner.

The adapter rejects pre-advanced creates, illegal stage jumps, operation-ID
changes, immutable input/slot/request changes, and replacement of an already
bound episode or generation reference before issuing SQL.

## Remaining activation gates

1. lease acquire/renew/release and expired-owner takeover semantics;
2. bounded recovery discovery using `FOR UPDATE SKIP LOCKED` or an approved
   equivalent;
3. real isolated PostgreSQL transaction/rollback/concurrency QA;
4. schema migration, backup/restore, capacity, observability, and rollback;
5. default-off runtime composition and shadow-mode reconciliation.

**P10 D3A-PG-CAS-ADAPTER PASS.**

