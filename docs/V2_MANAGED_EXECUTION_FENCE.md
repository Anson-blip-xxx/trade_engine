# Managed mutation boundary

The Testnet process factory accepts a transaction-held execution guard and wraps
its shared signed transport before building the account, execution, protection,
settings and exit components. Every non-GET call enters that guard before any
network call. The lock remains held until the response/exception has propagated.

OPEN covers opening orders and symbol settings. MANAGE covers reduceOnly=true
orders, closePosition=true conditional protection and scoped cancellations.
An ACTIVE route permits both; DRAINING permits only MANAGE. GET reconciliation
remains available to a fenced worker. Unknown mutation types default to OPEN and
are still subject to the transport's endpoint/parameter allowlist.

An acquisition failure raises SubmissionNotSent(EXECUTION_FENCED). A transaction
failure after network I/O is NETWORK_OUTCOME_UNKNOWN and must be reconciled,
never treated as proof that retrying a write is safe.

The executable startup requires epoch/worker-token binding for write-enabled
vault mode. Partial binding, legacy-source binding and invalid versions fail
closed. Ownership is checked before credential decryption/client construction.
Legacy startup remains compatible; the old installed services are disabled and
must not be mistaken for guarded workers.

Rotation through the Web is rejected for ACTIVE or DRAINING accounts, preserving
old-position management credentials. Trusted activation locks the registry row
and rechecks its binding, as the mutation guard already does.

Deployment update: the independent controller now uses this guard in the real
managed Testnet child. See `V2_TESTNET_EXECUTION_CONTROLLER.md` for actual process
recovery and target-rejection acceptance. Successful two-account handover still
requires funding the zero-balance Testnet target. LIVE remains disabled.

QA: 92 transport, ownership, rotation, runtime credential and process composition
regressions passed. A real temporary PostgreSQL lock-contention test confirms
that a draining transition cannot pass an in-flight request's shared route lock.
