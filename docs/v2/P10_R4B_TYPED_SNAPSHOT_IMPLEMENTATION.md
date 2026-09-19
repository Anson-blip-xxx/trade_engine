# P10 R4B-STATE-ACK - Typed Strict Snapshot CAS

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** The legacy
> `PositionStatePort.save_positions()` and `PositionStateService.save()` remain
> unchanged. No runtime caller uses this strict adapter yet.

## Outcome

`StrictRedisPositionSnapshotAdapter` adds an explicit, no-fallback read/CAS
boundary for the whole `pm:positions` compatibility snapshot.

Strict reads return:

- `FOUND`, with parsed positions and the exact raw Redis token;
- `NOT_FOUND`;
- `MALFORMED`;
- `UNAVAILABLE`.

Strict writes return:

- `APPLIED`;
- `ALREADY_APPLIED` for exact replay;
- `STALE` when another writer changed the snapshot;
- `MALFORMED`, `UNAVAILABLE`, `UNKNOWN`, or `INVALID`.

Unlike the legacy helper, this boundary never falls back to files, never
silently reports success, and never filters malformed rows into an apparently
valid partial snapshot.

## CAS And Retry Semantics

The caller must pass the `StrictSnapshotRead` obtained before planning. Lua
compares the exact raw token, or verifies that the key remains absent, before
setting the proposed snapshot.

If the current payload already equals the proposed canonical JSON, replay with
the original token is `ALREADY_APPLIED`. An exception from Lua is `UNKNOWN`
because Redis may have committed; an exact retry resolves a committed attempt.

Proposed snapshots must map normalized uppercase symbols to dictionary rows and
must be JSON-safe without NaN/Infinity. A forged FOUND token whose decoded
payload differs from its positions is `INVALID`.

## Compatibility Boundary

This is a parallel strict interface, not a change to the Phase 4/7 golden port:

- `execution.ports.PositionStatePort` still exposes only legacy load/save;
- `RedisPositionStateAdapter.save_positions` still swallows errors;
- `PositionStateService.save` still returns `None`;
- `_save` and `shared_executor._update_pos_cache` are not rewired.

Runtime adoption requires composing this adapter with the R4B snapshot-fence
plan and the exact canonical read tokens in one safe workflow. Until that
composition and caller acknowledgement propagation exist, active R4B migration
remains blocked.

## QA Contract

Isolated-Redis tests cover absent create, found update, concurrent stale write,
exact retry, ambiguous post-commit recovery, malformed state, forged token,
invalid snapshot shapes/values, and backend unavailability. Existing port
contract tests remain unchanged.

**P10 R4B-STATE-ACK PASS.** Typed snapshot acknowledgement is available as a
dormant boundary; runtime composition and protection verification remain
`IN_PROGRESS`.
