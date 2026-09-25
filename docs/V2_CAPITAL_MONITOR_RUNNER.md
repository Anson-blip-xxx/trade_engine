# Controlled SANDBOX monitor runner

Internal opt-in diagnostics only. No order placement, venue client, credential
loading, account switch, public authorization or LIVE execution is included.
Current Testnet services are not changed or restarted by this feature.

Apply `20260925_capital_monitor_runner.sql` after the capital-monitor migration,
once inside a migration transaction. Registry enrollment alone never starts work.
An account with no control row is disabled.

## Configuration and scheduling

`CapitalMonitorRunner.configure()` requires explicit enabled state, validated
`TransferMonitorPolicy`, expected control version and request UUID. Configuration
and immutable audit commit together. Exact retries return the original result;
changed requests or stale versions conflict. Request identity is tenant-wide, so
reuse against a different account cannot silently create a second configuration.
Replaying an old request returns its historical result, not a current-state read.

`run_one()` checks ownership and SANDBOX environment, acquires an account-specific
transaction lock, then rereads current control state and due time. Outcomes include
DISABLED, BUSY, NOT_DUE, SCANNED and SCAN_FAILED. Unconfigured or LIVE accounts are
never implicitly enabled. All returned outcomes retain execution_authorized=false.

`run_due()` selects a bounded tenant-specific batch (default 20, maximum 100),
then independently rechecks each account's controls. Selection is not an execution
permit: a disable committed after selection is honored. BUSY accounts are skipped
for this call and remain due. Hosts must budget repeated batches and database query
timeouts; this primitive is not an unbounded global account enumerator or a
fairness guarantee when an entire selected batch is persistently locked.

Successful scans persist the monitor's next due time. Failed scans persist a fixed
safe error code and retry at policy.retry_ms; they do not erase active incidents,
generate a false recovery, or expose exception text. A configuration version
change becomes immediately due without resetting monitor incident history.
Clock regression is rejected. Scans and run outcomes remain account scoped.

## Fencing and recovery

No expiring lease permits an old worker to commit after a new worker takes over.
The PostgreSQL transaction owns the account fence. Configure/disable uses the same
lock: a disable waits for already-running work to finish, and after it returns no
new governed scan can start under the previous configuration. It is not an
interrupt/cancellation command for work already in progress.

The monitor executes on the same connection in a savepoint. On a scan error only
that scan's effects roll back; the outer transaction records failure/retry. If the
outer transaction or connection dies, monitor changes, events, run history and
schedule all roll back together and the transaction lock is released by PostgreSQL.
Later workers can retry the unchanged due state. Audit/run rows are immutable.

Lock order is configuration namespace (configuration only), account runner fence,
then tenant capital lock (scan only). No network I/O is performed while these locks
are held. The separate notification sender remains independently configured.

## Explicit non-goals and rollout gates

- This governs this runner, not callers directly invoking lower-level scan APIs.
  Future composition must use this control path consistently.
- No systemd service/timer, automatic process start or real notification send is
  enabled. A host scheduler still needs explicit tenant scope, bounded connections,
  heartbeat/backlog monitoring and controlled Testnet rollout.
- Policy enrollment is internal, not an authenticated multi-tenant control API.
  Venue identity verification, application-role/RLS isolation and route ownership
  remain required before SaaS exposure.
- Safe **trading-account** switching needs its own final-submit fencing, draining
  and position/settlement acceptance. A monitoring switch does not satisfy that gate.

QA uses isolated PostgreSQL, including competing runners/configuration updates,
disable waiting on an account fence, savepoint errors and outer-transaction abort.
These are deterministic recovery tests, not a claim of deployed process-kill or
real A/B exchange acceptance.

Validation on 2026-09-25: 188 targeted tests passed across runner/monitor,
transfer/income/journal/registry, watchdog, trading health, account inventory and
daemon bootstrap. Ruff lint/format and staged whitespace checks passed. No fresh
full-suite run, runtime migration, process launch/restart, key change or real
Telegram delivery was performed. Existing services remained active.
