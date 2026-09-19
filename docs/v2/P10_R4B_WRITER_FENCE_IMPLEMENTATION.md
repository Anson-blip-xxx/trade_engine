# P10 R4B-WRITER - Legacy Snapshot Fence Planner

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** The planner
> is not wired into `pm:positions` writes. Active R4B migration remains blocked
> until migrated-row mutation paths use canonical CAS and surface typed failure.

## Outcome

`plan_legacy_snapshot_write` classifies one proposed whole-snapshot write using
an explicit authority/projection read pair for every symbol in the current and
proposed union. It returns an immutable, typed plan without performing IO.

The planner solves the batch-isolation problem: a mutation aimed at one
canonical symbol does not discard unrelated, still-legacy symbol updates.
Instead it produces a filtered snapshot and reports every fenced symbol and
reason.

## Rules

- No canonical authority or projection: allow the legacy row.
- Initial FLAT authority without a projection: allow the legacy row.
- Matching ACTIVE or QUARANTINED authority/projection: the legacy row must remain
  byte-equivalent at the Python value level. Mutation, deletion, or
  reintroduction is replaced with the current legacy row and reported as
  `ACTIVE_CANONICAL_MUTATION`.
- Matching FLAT authority/projection: a proposed legacy row is removed and
  reported as `FLAT_CANONICAL_REINTRODUCTION`.
- Missing read pairs, backend unavailability, malformed records, orphan records,
  or authority/projection episode, generation, provenance, alias, slot, or
  symbol mismatch fail the entire plan closed with no writable snapshot.

Inputs and nested output rows are copied; planning cannot mutate caller state or
retain nested aliases. The output and reason maps are read-only.

## Why It Is Not Wired Yet

The current `PositionStateService.save` intentionally swallows persistence
errors and returns `None`. Several lifecycle, monitoring, reconcile, and
`shared_executor` paths also assume a last-writer-wins full snapshot.

Wiring a filter under that API would silently discard a partial-close quantity
change or metadata mutation for a migrated ACTIVE position while the caller
continued as if persistence succeeded. Therefore runtime wiring requires:

1. typed save acknowledgement propagated to mutation-owning callers;
2. canonical projection CAS for migrated-row quantity and lifecycle changes;
3. removal of the direct `shared_executor._rset('pm:positions', ...)` writer;
4. explicit retry/reconcile behavior when a canonical CAS is stale or
   unavailable.

Until those gates are implemented, this planner remains a tested policy model,
not a production filter.

## QA Contract

Tests cover fully legacy batches, unchanged canonical rows, active mutation and
deletion, canonical-row reintroduction, unrelated-symbol update preservation,
FLAT cleanup, unavailable/malformed/orphan state, incomplete read sets, nested
alias isolation, and immutable outputs.

**P10 R4B-WRITER PASS.** This closes the pure batch-planning prerequisite only;
active R4B producer wiring remains `IN_PROGRESS`.
