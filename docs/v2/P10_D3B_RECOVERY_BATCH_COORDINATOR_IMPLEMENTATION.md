# P10 D3B - Recovery Batch Coordinator Boundary

> **IMPLEMENTED / CLOSED as a pure dormant planning boundary.** This is not the
> D3B runtime coordinator: it starts no worker, calls no handler, releases no
> lease, and performs no I/O.

`plan_recovery_batch` composes the acknowledged PostgreSQL claim result with
the typed recovery decision engine. It produces four explicit dispositions:

- `IDLE`: an acknowledged empty claim;
- `JOURNAL_UNKNOWN`: the batch write may have committed, so execute nothing
  until the journal is reconciled;
- `INVARIANT_VIOLATION`: malformed ownership, duplicate operation, duplicate
  slot, terminal row, or inconsistent claim shape; execute nothing;
- `READY`: exact immutable work items partitioned into automatic decisions and
  policy/operator-gated decisions.

The boundary requires one nonempty owner for the entire claimed batch, an
expiry on every record, unique operation IDs, at most one operation per slot,
and no terminal operations. A `CLAIMED` result with no records and an
`EMPTY`/`UNKNOWN` result with records are rejected fail-closed.

The planner deliberately does not turn a decision into a side effect. D3B
runtime work remains blocked on directive-specific ports/executors, D3C
exchange evidence resolution, D3D authority-read/revalidation composition,
scheduling policy, and activation gates.

**P10 D3B-RECOVERY-BATCH-BOUNDARY PASS.**
