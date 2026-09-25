# Durable unmatched-transfer monitor

Internal isolated-QA module; **not activated on the running Testnet service**.
Requires `20260925_capital_monitor.sql` after the capital transfer migrations.
No trading permission, automatic pair matching, account switching or fund transfer
is performed. A quiet monitor does not certify complete venue cash coverage.

## Scan and alert state

`CapitalTransferMonitor.scan()` reads account-scoped TRANSFER facts after their
currency baseline. Missing baselines, missing receipts and reversed pair/baseline
journals remain unresolved. Pre-baseline and future facts do not create false
overdue findings. SQL aggregates the whole scoped result, not a limited first page.

The caller supplies `now_ms` and a validated `TransferMonitorPolicy`. Defaults:

| Control | Default |
| --- | --- |
| Scan interval | 60 seconds |
| Event overdue age | 30 minutes |
| First alert confirmation | 2 minutes |
| Recovery confirmation | 3 minutes |
| Persistent-incident reminder | 60 minutes |
| Delivery retry delay | 60 seconds |

All intervals are explicitly configurable, finite and positive. The policy
snapshot is retained with each scan/event. Policy changes are evaluated even before
the prior next-scan deadline, without resetting the first-seen timer. Times are
epoch milliseconds; Telegram presentation uses UTC+8. Regressing clocks are
rejected instead of resetting timers.

PostgreSQL state retains next due time, confirmation/recovery timers and last
emission across restart. A tenant capital lock serializes scans with matching.
State and immutable OPEN/REMINDER/RECOVERED events commit atomically. A failed
query/transaction produces no false recovery. A new finding during recovery
cancels the quiet window. Repeated/concurrent scans cannot create duplicate OPEN
events for the same active incident.

This supplies durable scheduling **decisions**, not a new background process.
A future tenant-account runner must invoke due scans, monitor its own heartbeat
and check delivery backlog. No systemd timer or existing watchdog is changed here.

## Delivery

`deliver_latest()` takes an explicit account, stable destination UUID and a bounded
sender. Destination changes fail closed; no automatic fallback sends private
account alerts to a global chat. Configuration ownership and destination rotation
need an authenticated control plane before public SaaS use.

A separate account delivery lock prevents concurrent duplicate sends and does
not hold the capital scan lock over network I/O. Pending older states coalesce to
the newest event: after a long outage recovery can supersede unsent old alarms,
while every original event remains in immutable audit history. Only an exact
`True` acknowledgement advances the delivery cursor. Exceptions/false responses
retain pending state and persist retry delay; raw transport exceptions are not
stored. The chosen callback must enforce an I/O timeout.

`CapitalMonitorTelegram` validates tenant/account/destination and venue environment
against its explicit internal binding, then reuses `TelegramOperationalSink`'s
bounded transport and `CAPITAL_TRANSFER_OVERDUE` allowlisted template. No balance,
credential, arbitrary exception or raw venue payload is included in the message.
The UUID alone is not proof of chat ownership or authenticated authorization.

Telegram sends are **at least once**: a crash after remote success but before the
local acknowledgement commits may duplicate delivery. Stable internal event IDs
allow audit, not a claim of Telegram server-side idempotency. This layer reduces
repeat alarms and retries but cannot promise exactly-once external delivery.

## Remaining rollout gates

- Verified tenant/account identity and controlled destination enrollment.
- Authenticated dynamic policy management and application-role/RLS protection.
- Runner activation, scan heartbeat/backlog monitoring and Testnet observation.
- Full cash coverage, automatic evidence acquisition and safe account switching.

Tests use isolated PostgreSQL and fake Telegram transport; no real TG messages or
runtime database writes are part of this feature's QA.

Validation on 2026-09-25: 178 targeted tests passed across the monitor, capital
transfer/income/journal/registry, watchdog, trading health, inventory and daemon
bootstrap suites. Ruff lint/format and staged whitespace checks passed. This is
not a fresh full-suite run. Runtime services remained active with no restart;
no migration or real Telegram delivery was performed.
