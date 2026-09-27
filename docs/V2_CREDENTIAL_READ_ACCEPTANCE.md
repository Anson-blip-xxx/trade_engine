# Encrypted credential signed-read acceptance

`check_encrypted_credentials` resolves the pinned account/tenant/version from the
vault, retaining the registry SHARE lock through a bounded signed GET and receipt
commit. The only request is `GET /fapi/v1/positionSide/dual` on the fixed SANDBOX
transport. All trading/cancellation/protection/settings writes are explicitly off.
The existing private request budget is reused; there is no rate-limit bypass.

A valid boolean `dualSidePosition` response records SIGNED_READ_ACCEPTED. A failed
request or unexpected shape records SIGNED_READ_FAILED. Safe diagnostics include
only the transport's category and numeric HTTP/exchange codes; no response bodies,
signed URLs, exception text, credential fingerprints, balances or positions persist.
Audit events are immutable. Wrong account/version or a decryption error stops
before exchange I/O. A receipt does not prove distinct venue identity, authorize
trading, permit deletion of the source or complete an execution-account switch.

## Operational progress

The existing Testnet account has been imported with its original historical scope
and encrypted key/secret reference. Registry, identity guard/capital foundation,
vault and credential-check migrations were applied transactionally. Financial
history was not rewritten and no opening gate or new runner was activated.

An explicit NOLOGIN, non-superuser vault operator role was provisioned for the
internal import/check path. It has SELECT/INSERT on tenant/account/registry audit/
vault/check tables and column-level UPDATE(version) to allow the registry SHARE
lock. It has no financial order-table writes. Membership is not inherited by the
existing app role; the probe explicitly selects the role within its transaction.
This is a trusted operator migration role, NOT a public multi-tenant/RLS boundary.

The actual signature acceptance is **blocked**: the encrypted-only probe returned
HTTP 401 / exchange code -2015. A separate read-only control using the old deployed
reader and original credential file returned the same error. Encryption/readback
and import succeeded, but private API acceptance did not. The precise external
cause (key status, IP restriction, permissions/environment) is not established.
The original trading processes and credentials remain unchanged. Do not infer
private-account health from process liveness or healthy public market ingestion.

No source was deleted, no API permissions relaxed, no LIVE request issued, and no
exchange write executed by the migration/probe. Resume acceptance after the
Testnet key/access issue is resolved; then qualify runtime/rollback and remove
plaintext copies only after every relevant consumer has migrated.

## QA

85 isolated tests pass across signed-read acceptance, safe numeric diagnostics,
immutable receipts, import, runtime binding, console and daemon entry. Production
role checks additionally confirm INSERT/UPDATE on the order table are denied.
These passing tests do not override the failed real exchange acceptance.
