# P10 R4B-COMPOSE - Atomic Fenced Snapshot Commit

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No runtime
> caller uses this adapter. The running legacy snapshot writers, services, and
> deployment configuration are unchanged.

## Outcome

`RedisFencedSnapshotCommitAdapter` composes the R4B-WRITER planner with strict
whole-snapshot acknowledgement in one commit protocol. It reads the exact raw
tokens for `pm:positions` and every authority/projection record in the batch,
plans the compatibility write, then submits all observed tokens to one Lua
script.

The script checks every canonical token before checking or writing the legacy
snapshot. Any intervening authority, projection, or snapshot mutation returns
`STALE`; it never mutates canonical records. This closes the planning-to-write
TOCTOU window left between the dormant planner and the standalone strict CAS.

## Batch And Fence Contract

- `slots` must exactly cover the union of current and proposed symbols.
- Canonical reads are strict: malformed, unavailable, orphaned, or
  slot-mismatched records reject the complete write.
- ACTIVE or QUARANTINED canonical symbols preserve their current legacy row.
- FLAT canonical symbols cannot be reintroduced into the legacy snapshot.
- Symbols with no canonical ownership retain legacy snapshot behavior.
- The applied payload is the planner output, not the unfiltered proposal.

The adapter returns typed `APPLIED`, `ALREADY_APPLIED`, filtered variants,
`STALE`, `FENCE_REJECTED`, `MALFORMED`, `UNAVAILABLE`, `UNKNOWN`, or `INVALID`.
An exception after the Lua attempt is `UNKNOWN`; an exact retry can resolve a
committed attempt as `ALREADY_APPLIED`.

## Compatibility Boundary

The legacy `PositionStatePort`, `PositionStateService`, `_save`, monitor,
reconcile, and strategy writers are not rewired. The standalone
`StrictRedisPositionSnapshotAdapter` also remains available and unchanged.
This node supplies the atomic persistence primitive only.

Active R4B migration remains `IN_PROGRESS`. Before runtime cutover, callers
must propagate typed acknowledgements and retry/UNKNOWN policy, lifecycle
mutations must use the canonical fenced primitives, and exchange protection
must be verified for the exact episode/generation.

## QA Contract

Disposable-Redis tests cover legacy apply, active filtering, FLAT removal,
canonical and snapshot races, filtered replay, ambiguous acknowledgement
recovery, orphan rejection, exact batch coverage, and backend failure.
Phase 10 fact tests freeze Lua key ordering, snapshot-only mutation, strict
slot coverage, planner composition, dormant wiring, and roadmap status.

**P10 R4B-COMPOSE PASS.** Atomic fence/CAS composition is complete and dormant;
caller acknowledgement propagation, lifecycle wiring, and protection
verification remain before the active R4B producer cutover.
