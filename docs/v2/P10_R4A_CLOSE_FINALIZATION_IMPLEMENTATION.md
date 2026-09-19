# P10 R4A-CLOSE - Canonical Close Finalization

> **IMPLEMENTED / CLOSED on isolated V2.** This closes the canonical
> ACTIVE-to-FLAT release gate for the native-open producer only. R4 remains in
> progress: migrated and replacement producers are not wired, and V2 is not
> deployable.

## Outcome

The V2 lifecycle now captures the exact canonical identity before attempting a
close and transitions slot authority to `FLAT` only after Binance reports that
the position is absent. The same rule covers lifecycle close, ghost cleanup,
and silent reconciliation.

Capture requires all of the following evidence:

- ACTIVE authority with `NATIVE` provenance;
- matching authority and projection episode/generation;
- local side, system, entry, open time, and positive remaining quantity matching
  the projection;
- explicit configured `ACCOUNT_PRINCIPAL_ID` and the existing PROD/DEMO
  environment selection.

The captured fence contains the slot key, episode ID, slot generation,
authority revision, and projection revision. Finalization uses the authority
adapter's atomic ACTIVE-to-FLAT CAS with the captured episode, generation, and
revision. A retry that observes exactly the resulting FLAT revision is treated
as already applied. Any newer episode, generation, revision, malformed record,
or unavailable backend fails closed.

## Exchange-Flat Gates

Finalization is invoked only after direct exchange evidence:

1. lifecycle finds no matching position before submitting a close order;
2. lifecycle submits the close and the follow-up `positionRisk` read is flat;
3. ghost cleanup's global `positionRisk` snapshot omits the symbol, after its
   per-symbol distributed lock is acquired;
4. reconciliation's valid `positionRisk` snapshot omits a locally tracked
   symbol.

Rejected orders, exceptions, no-fill responses, partial fills, system-filtered
ghosts, lock failures, malformed API responses, sandbox paths, and symbols still
present at the exchange never release authority. Finalizer failures are logged
and cannot turn a failed close into success or delete an otherwise retained
position.

## Compatibility And Limits

- With no configured principal, canonical capture is disabled and legacy V2
  close behavior is preserved.
- Only `NATIVE` authority is releasable by this gate. Migrated or reconstructed
  positions require their own controlled producer/authority policy.
- Projection and desired-protection records are retained after FLAT as fenced
  history. The atomic native-open handoff replaces them only when allocating a
  later generation.
- Authority release is intentionally best-effort relative to legacy accounting:
  a Redis outage leaves the slot ACTIVE and blocks reopen rather than risking an
  ABA release of a newer position.
- This change was not deployed, did not restart services, and did not access
  production Redis, databases, or trading APIs.

## QA Contract

Tests cover isolated-Redis capture/finalize, idempotent retry, stale-fence ABA
rejection after reopen, every local identity mismatch, backend unavailability,
normal close, already-flat close, partial and rejected close exclusion,
ghost/reconcile wiring, exchange-present exclusion, strict Redis wiring, and
architecture/documentation facts.

**P10 R4A-CLOSE PASS.** Same-slot native reopen is no longer permanently
blocked after a confirmed flat close. R4 remains `IN_PROGRESS` until the
remaining controlled producers and replacement-generation flows pass QA.
