# Owner Web credential modification

Select an account and use the API Key / Secret modification form. Both values
are required and never prefilled or returned by the API. Switching selected
accounts clears pending secret fields. Only credentials for the **same actual
venue account and environment** belong here; another account needs enrollment.
Venue identity is not yet independently proven, so rotation is not readiness.

`POST /api/accounts/{registry}/credentials` accepts exactly request_id,
expected_version, api_key and api_secret under the existing authenticated HTTPS
and CSRF boundary. It cannot change tenant, environment, account ID or alias.
The operation atomically creates a new encrypted vault version, CAS-updates the
registry reference and writes the existing immutable rotation audit event.
Concurrent stale updates fail; exact request retries are idempotent. Changed
secret data on the same request conflicts. Retired/foreign accounts are rejected.

No old ciphertext, historical order or verification audit is erased. Only the
current binding version's checks are shown, so a modified key requires fresh
verification. LIVE can be stored but is not contacted or activated. The endpoint
does not restart workers, hot-reload credentials, switch accounts or authorize
trading. Existing stopped Testnet services remain stopped.

QA covers atomic rollback, concurrent winner, idempotent replay, scope and CSRF,
retired accounts, no-secret GET, preserved history and validation invalidation.
