# User-authorized Testnet cleanup — 2026-09-27

There was one imported and one Web-enrolled Testnet registration, not two new
Web Testnet accounts. The imported binding failed signed reads; the Web binding
passed. LIVE credentials were excluded from all operations.

At cleanup time the previously observed two positions were already absent.
No market close was therefore submitted. One orphan TAKEUSDT conditional order
was canceled by exact exchange ID after confirming the account was flat.
The existing ScopedCleanup worker persisted CAS-before-send and confirmed
cancellation evidence in PostgreSQL. Final reads showed zero positions, zero
ordinary orders and zero conditional orders. This is a point-in-time result.

The legacy Testnet daemon and watchdog were stopped and disabled to prevent
reopening under old credentials. The daemon exceeded its shutdown timeout;
systemd completed termination and MainPID was zero before clearing its failed
state. Both processes were inactive before the exchange write. No new trading
worker was started, and no LIVE service or account was activated.

The failed imported registration is logically removed via an immutable retirement
marker: hidden from the available-account list and rejected by the new vault
runtime resolver. Historical identity, ledger, aliases and encrypted evidence
remain recoverable/readable; no financial rows or old credential files are erased.
This is not crypto-shredding or deletion of an exchange account/API key. Older
code does not implement the retirement check, so its disabled services must not
be restarted. A future new worker requires separate identity and handover checks.

QA: 80 credential/console/runtime/read-acceptance/cleanup regressions passed,
including retired-account hiding, rejected credential resolution, preserved
history, close-only maintenance and no resend on ambiguous exchange results.
