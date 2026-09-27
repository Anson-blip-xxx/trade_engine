# Owner account Web entry

The HTTPS dashboard mounts `/accounts` and `/api/accounts` on a separate,
loopback-only owner service. Existing dashboard authentication is required;
Nginx overwrites the authenticated identity header. This is a single-owner
console, not public multi-tenant SaaS authentication.

Both SANDBOX and LIVE can be explicitly enrolled for **encrypted storage only**.
Enrollment does not read legacy config, validate exchange access, activate an
account, or switch a trading process. Stats selection only changes the view;
new enrollment does not automatically merge prior account history.

API Key and Secret use AES-256-GCM with environment/tenant-bound authenticated
data. The existing systemd encrypted master is delivered on a protected memory
mount. Host-root compromise is outside this protection; the host wrapping key
is not hardware-backed. No plaintext credential endpoint or browser storage.

The dedicated peer-authenticated database role can enroll credentials and edit
aliases, but cannot mutate trade intents or settlements. Request body buffering
is bounded above the maximum accepted body; proxy request buffering and access
logging are disabled. The service has core dumps disabled and fixed diagnostics.
Existing legacy credential files are not removed or migrated by this rollout.

Deploy the locked dedicated venv and immutable release, set the root-owned
account-admin.env with V2_ADMIN_TENANT, V2_ADMIN_OWNER and V2_ADMIN_ORIGIN,
install the unit and include account-admin-location.conf in the HTTPS server.
Keep the existing trading/watchdog/dashboard services unchanged.

QA: console database/HTTP isolation and frontend stale-response regressions:
21 passed. Default HTTP handler rejects execution-switch requests; the internal
preparation API requires explicit opt-in and is not mounted by this service.
