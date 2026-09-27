# Execution switching — current status

Updated 2026-09-27 (UTC+8). The independent Testnet controller and real guarded
child pipeline are deployed; this is no longer a request-only Web implementation.
See `V2_TESTNET_EXECUTION_CONTROLLER.md` for architecture and acceptance evidence.

Web appends immutable, versioned desired-account requests. It cannot write the
actual execution route or worker token. A separate least-privilege controller
owns actual transitions, locks a tenant/environment lease and requires child
READY before activation. Requests are idempotent and stale epochs are rejected.

Every signed mutation in the managed child passes a transaction-held ownership
guard. Draining allows management only. Target preflight rejection preserves a
healthy source. Successful switching requires a clear exchange inventory and
local ledger, confirmed old-child termination, and a fresh target preflight.
Each account retains its own capital/risk baseline when revisited.

Actual acceptance confirmed account A activation, account B rejection without
interrupting A, restart into DRAINING, and explicit resume of A. Entries remain
disabled for acceptance. B has zero simulated wallet/margin/available balance;
successful A -> B -> A remains blocked until B has Testnet funds. The disabled
legacy daemon/watchdog were not restarted. An isolated regression run passed
88 tests; this does not substitute for the blocked exchange handover test.

LIVE credential storage and view/request separation do not implement production
execution. Isolated LIVE worker/database/cache deployment and its acceptance
remain unfinished. The Testnet controller never consumes LIVE requests.
