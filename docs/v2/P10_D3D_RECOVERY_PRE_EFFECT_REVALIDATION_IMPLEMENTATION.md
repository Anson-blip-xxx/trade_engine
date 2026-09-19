# P10 D3D - Recovery Pre-Effect Revalidation

> **IMPLEMENTED / CLOSED as a dormant read guard.** This is not a mutation
> claim, executor, exchange lock, Redis transaction, or runtime activation.

A successful `RecoveryGenerationReader.resolve` now returns an immutable
`GenerationWitness` containing the exact canonical authority record and, for
protection work, the exact desired-protection record observed during planning.
`revalidate` performs the same injected reads again and requires:

- the witness belongs to the same operation ID and operation type;
- the current pure generation fence still passes;
- the complete canonical records, including their revisions and statuses, are
  exactly equal to the planning witness.

Typed read failures remain typed. A current fence failure is returned directly.
If generations still match but either canonical record changed, the result is
`CANONICAL_CHANGED`; the caller must abandon the plan and rebuild it.

## Deliberate limit

This guard narrows but cannot eliminate the gap between the last local read and
an external exchange call. It also does not make the separate authority and
desired reads an atomic Redis snapshot. Active destructive protection work
still requires the D3D mutation claim/fencing protocol, V1/V2/V3 checks, lease
validation, ambiguous-effect reconciliation, and directive-specific executor
gate. `PASSED` is evidence for that later gate, never standalone permission to
cancel, create, or write back.

## QA boundary

Tests prove exact unchanged-witness acceptance, authority revision drift,
desired revision drift, current typed failures, wrong-operation witnesses,
exact second-read counts, and absence of concrete backend/runtime imports.

**P10 D3D-RECOVERY-PRE-EFFECT-REVALIDATION PASS.**
