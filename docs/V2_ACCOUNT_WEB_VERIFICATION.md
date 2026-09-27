# Web Testnet verification

The existing owner console now supports explicit environment filtering and a
Testnet-only `POST /api/accounts/{id}/verification` with `expected_version`.
The corresponding GET reads the latest receipt for the current binding; it does
not contact Binance. Exact owner authentication, Origin and custom action header
requirements are unchanged. Request-supplied tenant identities are rejected.

The POST pins the credential version and rejects LIVE, foreign and retired
accounts before resolving secrets or network I/O. Per-account transaction locks
prevent concurrent checks. The last result is reused during the configurable
cooldown (`V2_VERIFY_COOLDOWN_SECONDS`, default 60); freshness defaults to 300
seconds (`V2_VERIFY_FRESHNESS_SECONDS`). Both settings are bounded 1–3600 seconds.
Receipts from older credential versions are not displayed as current results.

The only exchange operation is a bounded signed Testnet GET of position mode.
All transport trading/cancel/protection/settings switches remain false. Private
request weight is charged through the existing account-scoped PostgreSQL quota.
Results are immutable audit receipts with allowlisted numeric diagnostics.
No secrets, signed URLs or raw exchange responses are sent to the browser.

The UI shows the check time in UTC+8, outcome and stale/cached state. Generation
checks discard previous-account statistics and verification responses after an
account/environment switch. Production verification is disabled on both sides.

**Not delivered by this milestone:** actual execution-account activation,
worker handover, proven venue identity, live readiness or order-cycle acceptance.
The execution switch button is visibly disabled, not a simulated success.
The stopped legacy Testnet worker/watchdog remain stopped. A successful check
must never be interpreted as authorization to start a trading worker.

The service role gains only credential-check INSERT/SELECT, quota access and
column-level registry UPDATE(version) required by PostgreSQL SHARE locks. It
still cannot write orders or settlement records, manage systemd, or execute
exchange writes through this endpoint.

## Deployed acceptance — 2026-09-27

66 isolated regression tests passed. On the deployed owner backend, both active
Testnet bindings were checked: the earlier Web enrollment passed; the newly added
enrollment returned HTTP 401 / exchange -2015. Both produced auditable receipts;
repeat requests reused the cooldown result, and a mismatched Origin returned 403.
No LIVE credential was resolved or sent. The legacy trading daemon/watchdog
remained inactive with no worker PID. Consequently two-account execution-switch
acceptance is still blocked; this milestone is not a completed account handover.
