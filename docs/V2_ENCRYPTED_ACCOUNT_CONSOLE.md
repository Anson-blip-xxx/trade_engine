# Encrypted account console — candidate, not deployed

The current production/Testnet daemon still reads the existing protected plaintext
credential file. This implementation does NOT make that file encrypted, migrate
its keys or claim that the operational no-plaintext requirement is complete.
Do not expose a credential-entry endpoint until all deployment gates below pass.

## Implemented boundary

- Both API key and secret are encrypted before SQL insertion, using AES-256-GCM,
  a fresh 96-bit random nonce and a 256-bit master supplied by an external provider.
- AAD binds format version, tenant, credential reference, environment and master
  key version. Tampering, wrong key, row substitution or missing master fails closed.
- PostgreSQL stores ciphertext/nonce/key version only. Business registry and audit
  records hold opaque references, never key/secret plaintext or key fingerprints.
- Nonce reuse under the same key version is rejected by a unique DB constraint.
  Immutable credential versions preserve old-account management; no destructive
  secret overwrite. A new ref is required for changed key material.
- Atomic encryption + account enrollment uses a server-bound tenant and an
  idempotent request ID. Replays with changed secrets fail. Enrollment is always
  unverified and never grants trading permission. Two keys are NOT proof of two
  exchange accounts; duplicate underlying venue identity must still be checked.
- Aliases use separate versioned metadata and immutable audit, not a rewrite of
  historical account IDs or credential-binding versions.
- Per-account summary and latest 100 trades resolve the complete account scope
  through the server-owned registry. All-time final-settlement USDT PnL is distinct
  from wallet equity, mark-to-market or deposit-adjusted performance. Those richer
  measures are not invented here. The old aggregate dashboard is unchanged.

The crypto API follows the authenticated-encryption contract documented by
[PyCA cryptography](https://cryptography.io/en/49.0.0/hazmat/primitives/aead/).
There is no custom cipher and no Base64-as-encryption scheme. Master rotation
rewrapping, key deletion/revocation and key-provider recovery are not implemented.

## Web candidate

`services/v2_account_admin_http.py` supplies an **unmounted** handler; assets are
in `web/v2_accounts`. The old read-only handler has no new mutation route.
The account manager supports encrypted add, alias update, switching the selected
statistics view and requesting a switch preparation. UI clearly states that this
is not a change of active execution account. Late view responses are discarded.
Inputs are cleared after submission; no secrets in browser storage, query strings,
responses, access logs or audit. Plaintext is transiently required in browser and
server memory; Python/JS cannot guarantee zeroization, and browser extensions or
a compromised host are outside an at-rest encryption guarantee.

The handler must be constructed from a **server-authenticated owner mapping**.
It accepts only a loopback trusted proxy and exact `X-V2-Authenticated-User`;
the proxy MUST overwrite that header with its authenticated identity, not forward
the client value. The existing reverse proxy currently strips Authorization and
does NOT supply this header, so this candidate is intentionally not ready to mount.
The backend must remain unreachable externally. Local code execution remains a
trusted boundary; this is not yet hostile multi-tenant hosting or database RLS.

Writes require exact configured HTTPS Origin, JSON, a custom action header and
bounded request size; no permissive CORS. Replies are no-store with restrictive CSP.
No HTTP endpoint resolves plaintext credentials. Errors never reflect submitted
body or provider/database exception text. Required future proxy controls: request
body memory-only buffering/no temp-file spooling, disabled body/access/debug logs,
request timeouts, authentication rate limits, TLS and session security tests.

## Activation remains blocked

`POST /api/account-switches` only creates REQUESTED; it does not drain A, activate
B, switch keys, release budgets or cancel orders. A and B require distinct verified
venue identity/readiness, exclusive runner ownership, and account-bound signing.
Old A must retain its encrypted key version for protection/exit/settlement until
fully reconciled. UI view selection never authorizes any trading transition.

## Rollout prerequisites (not yet done)

1. Apply registry, alias/vault and switch migrations using restricted application
   roles. Provision an independent master-key provider, e.g. KMS or systemd encrypted
   credentials with host/TPM protection. Do not put master bytes in PostgreSQL,
   Git, env files, backup archives or logs. Fail startup if unavailable; no fallback.
2. Build the separately locked and audited cryptography runtime. The optional
   requirements file is not part of the current production dependency preflight.
3. Register the existing account with its original historical scope; atomically
   import its API key/secret into the vault and validate signing without changing
   venue identity. Wire every signing/recovery process to the vault.
4. Only after verified encrypted-path operation remove plaintext copies from the
   active configuration and handle backups, snapshots and possible swap/core dumps.
   Do not erase the old account's only usable signing key before validation. A plain
   file with mode 600 alone does NOT satisfy encrypted storage.
5. Mount the authenticated HTTPS handler, verify forged proxy headers, cross-owner
   detail access, CSRF, body spooling/logging and real browser flows. No public
   multi-tenant claim until those checks pass.
6. Complete the existing A/B switch and two-distinct-Testnet-account acceptance
   roadmap before enabling an execution-switch control.

## Validation

55 isolated PostgreSQL/account/registry/switch/drain/dashboard/report tests pass
with cryptography 49.0.0 in a temporary QA virtualenv. A separate executable Node
test confirms a late A response cannot replace B's selected statistics (1 passed).
Ruff, JS syntax and whitespace checks pass. This is not a live browser/TLS proxy,
real-key migration or two-venue-account execution acceptance result. No production
dependencies, credential files, master keys, migrations or services were changed.
