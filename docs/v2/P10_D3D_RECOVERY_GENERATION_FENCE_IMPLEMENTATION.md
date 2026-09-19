# P10 D3D - Recovery Generation Fence

> **IMPLEMENTED / CLOSED as a pure dormant decision.** It reads no Redis key,
> performs no CAS, calls no exchange API, and is not runtime-wired.

`validate_recovery_generation` relates one operation to an already-read
canonical `SlotAuthority` and, for protection work, an already-read
`DesiredProtectionRecord`.

Rules:

- close, partial-close, and ghost-finalize require the exact retained episode
  ID and slot/lifecycle generation;
- protection create/replace additionally requires ACTIVE authority, exact
  protection generation, exact `last_operation_id`, and a non-terminal desired
  status;
- quarantined authority, missing bindings, slot/key mismatch, stale episode,
  stale protection, or missing desired state fail closed;
- a fresh OPEN on FLAT returns `ALLOCATION_REQUIRED`, not permission to submit;
- OPEN on ACTIVE returns `POLICY_REQUIRED` because duplicate/scale-in behavior
  is unresolved;
- EXTERNAL_RECONCILE always returns `POLICY_REQUIRED` because restart cannot
  infer adoption/quarantine/business-close policy.

`fence_passed=True` means only that the supplied canonical generations match.
It is not side-effect authorization. A caller must also satisfy the typed
recovery decision, current lease, exchange evidence, and directive-specific
executor gate.

The injected `RecoveryGenerationReader` now performs one typed canonical
authority read and, for protection operations only, one typed desired-state
read before applying this decision. NOT_FOUND, MALFORMED, UNAVAILABLE, and
invalid adapter responses remain distinct and fail closed. It imports no
concrete Redis adapter and performs no write.

Remaining D3D work is exact pre-side-effect revalidation and composition with
directive executors.

**P10 D3D-RECOVERY-GENERATION-FENCE PASS.**
