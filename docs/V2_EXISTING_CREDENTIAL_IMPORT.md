# Existing account credential import

`import_existing_credentials` is an internal SANDBOX-only migration primitive.
It preserves the supplied historical full AccountScope; it does not allocate a
new trading account ID as the web new-account enrollment does. It requires an
existing tenant and the registry/vault migrations in the destination database.

The caller supplies the credential material transiently. Encryption, registry
enrollment, immutable registry audit and decrypt/readback verification share one
PostgreSQL transaction. Failed readback or conflicting enrollment rolls back both
new rows. The request ID deterministically addresses an encrypted reference;
concurrent retries are idempotent and changed secret material is rejected.

Accounts already registered under another request or owner are rejected rather
than silently rebound. Existing trades and ledgers are never rewritten. Import
reports `IMPORTED_UNVERIFIED`, `execution_authorized=false` and
`source_cleanup_authorized=false`. Neither this status nor the crypto roundtrip
proves venue ownership or a completed operational migration. No source file is
read, deleted or modified by the primitive itself.

## Server acceptance performed

The real systemd credential mount on this host uses root-owned files with named
service-UID ACLs: apparent 0440/0550 mode bits represent the ACL mask, while group
and other entries have zero permissions. The reader was updated to validate the
exact ACL entries, not merely allow broader group permissions. Real transient-unit
tests pass with the descriptor verified on a read-only tmpfs with `noswap`.

A separate V2 master was generated through an in-memory pipe and saved using
systemd encrypted credentials, mode 0600, root owned. A transient service loaded
it with `LoadCredentialEncrypted` and passed AES-GCM roundtrip validation without
printing any key, nonce/ciphertext samples or key fingerprints. No actual exchange
credentials were used for that test. The transient QA units have exited.

Protection mode is **host-key encryption, not TPM-backed**. The root-only systemd
wrapping key is mode 0400 on unencrypted host media. The V2 master and database API
credentials are encrypted at rest, but host-root compromise or a backup containing
all ciphertext and wrapping material defeats that isolation. Separate protected
backup/restore handling must be completed before actual account cutover. This is
not a claim that all key hierarchy material is hardware sealed.

## Acceptance and remaining work

82 isolated tests pass: runtime credentials, vault/console, registry, imports,
history association, concurrent replay, failed readback rollback, foreign account
rejection and strict ACL positive/negative cases. The real systemd delivery and
encrypted-master roundtrip are additional host acceptance checks, not mocked tests.

The initial primitive delivery did not import runtime API keys. The subsequent
operator import now preserves the original scope with encrypted material and a
restricted migration role; see `V2_CREDENTIAL_READ_ACCEPTANCE.md` for the current
signed-read rejection and rollout status. Trading processes still use their prior
release/plaintext source. Nothing was deleted or switched, and the web console
and execution-account switching remain unactivated.
