# P10 R4B-ACK-POLICY - Typed Snapshot Caller Decisions

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** This node
> defines caller control flow but does not replace `_save`, the legacy
> `PositionStatePort`, or any monitor/reconcile/lifecycle caller.

## Outcome

`FencedSnapshotCommitService` preserves the complete
`FencedSnapshotCommitResult` and converts its code into one mandatory caller
action. It never coerces a failed or ambiguous write into success and never
swallows an unexpected port exception.

| Commit result | Caller action |
|---|---|
| `APPLIED`, `ALREADY_APPLIED`, filtered variants | `ADVANCE` |
| `STALE` | `RELOAD_AND_REPLAN` |
| `UNAVAILABLE` | `RETRY_WHEN_AVAILABLE` |
| `UNKNOWN` | `RESOLVE_UNKNOWN` |
| `FENCE_REJECTED`, `MALFORMED` | `QUARANTINE` |
| `INVALID` | `REJECT` |

Only `ADVANCE` is an acknowledged lifecycle commit. Every other action blocks
the lifecycle stage represented by that snapshot write.

`RESOLVE_UNKNOWN` means repeat/query the same idempotent snapshot effect until
the adapter yields `ALREADY_APPLIED`, `STALE`, or a known failure. It never
authorizes a new exchange operation. `RELOAD_AND_REPLAN` requires new snapshot
and canonical tokens; callers must not replay a stale plan.

## Compatibility Boundary

The service is dormant and has no runtime caller. Existing `_save` sites still
return `None` and preserve their frozen legacy behavior. Active R4B migration
therefore remains `IN_PROGRESS` until selected lifecycle callers use this typed
decision, persist/retry incomplete work, and verify current-generation exchange
protection.

## QA Contract

Unit tests exhaustively map every commit code, prove only acknowledged outcomes
permit lifecycle advance, preserve the exact result/input objects, and prove
unexpected port failures are not swallowed. Phase 10 facts keep the service
dormant and freeze the no-implicit-retry policy.

**P10 R4B-ACK-POLICY PASS.** The caller decision contract is ready; runtime ACK
propagation, lifecycle wiring, and protection verification remain open.
