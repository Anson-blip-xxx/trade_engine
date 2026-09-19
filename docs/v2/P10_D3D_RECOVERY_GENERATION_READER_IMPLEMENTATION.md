# P10 D3D - Recovery Generation Reader

> **IMPLEMENTED / CLOSED as a dormant read boundary.** No concrete Redis
> adapter, database connection, exchange API, mutation, or runtime path is
> added.

`RecoveryGenerationReader` resolves the canonical records required by the pure
recovery generation fence through injected ports:

- every operation performs exactly one canonical authority read;
- protection create/replace additionally performs exactly one desired-state
  read;
- close, partial-close, and ghost-finalize never read desired protection;
- authority and desired NOT_FOUND, MALFORMED, and backend-unavailable outcomes
  are preserved as distinct typed resolution codes;
- exceptions, untyped responses, and inconsistent FOUND responses fail closed;
- only a complete typed read set reaches `validate_recovery_generation`.

`PASSED` only means the supplied canonical generations match. It does not
authorize an exchange call, journal transition, Redis mutation, or any other
side effect. The executor must still re-read/revalidate immediately before its
effect and satisfy the lease, evidence, and directive gates.

## QA boundary

Unit tests prove exact read counts, protection-only desired reads, all typed
failure mappings, exception handling, malformed injected responses, and stale
desired-generation rejection. Architecture fact tests keep concrete backends
and active runtime packages outside this module.

## Remaining work

- define an exact pre-effect revalidation token/window;
- compose it with directive-specific executor ports;
- keep runtime activation default-off until D3C evidence and product policies
  are approved.

**P10 D3D-RECOVERY-GENERATION-READER PASS.**
