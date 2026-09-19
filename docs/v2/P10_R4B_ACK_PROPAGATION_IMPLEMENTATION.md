# P10 R4B-ACK-PROPAGATION - Lifecycle Continuation Boundary

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** Existing
> `_save`, `PositionStatePort`, monitor, reconcile, and lifecycle runtime paths
> remain unchanged.

## Outcome

`AcknowledgedSnapshotLifecycleService.commit_and_advance` performs exactly one
typed fenced snapshot commit. It invokes the injected lifecycle continuation
only after `APPLIED`, `ALREADY_APPLIED`, `FILTERED_APPLIED`, or
`FILTERED_ALREADY_APPLIED` has been classified as `ADVANCE`.

All other outcomes return without invoking the continuation:

- `STALE` requires reload and replan;
- `UNAVAILABLE` requires durable retry policy;
- `UNKNOWN` requires same-effect acknowledgement resolution;
- fence rejection or malformed state requires quarantine;
- invalid input is rejected.

Invalid callback/service contracts fail before lifecycle advance. Commit and
continuation exceptions propagate unchanged; the boundary contains no loop,
sleep, retry, fallback, or exception swallowing.

If the snapshot commit succeeds but the continuation raises, a later caller
may observe `ALREADY_APPLIED` and invoke it again. The selected continuation
must therefore be idempotent under the operation/episode identity; this seam
does not falsely claim distributed exactly-once execution.

## Remaining Boundary

This service is dormant and has no runtime caller. A future selected lifecycle
stage must supply durable recovery for blocked outcomes and idempotency for its
continuation before wiring. Existing legacy `_save()` remains intentionally
unacknowledged. Exchange protection verification and the D2C activation gate
remain mandatory before active migrated-position production.

**P10 R4B-ACK-PROPAGATION PASS.** Caller continuation can no longer advance
through a non-acknowledged snapshot when it uses this boundary.
