# P10 R4B-RECOVERY-HANDOFF - Blocked Outcome Durable Port

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No Redis/PG
> backend, journal schema, runtime caller, retry loop, or production behavior
> is introduced.

## Outcome

`RecoverableSnapshotLifecycleService` composes the acknowledged lifecycle
boundary with an injected recovery handoff port. Acknowledged snapshot commits
continue normally and never create recovery work. Every blocked commit action
hands off exactly once with the same operation ID, typed acknowledgement,
proposed snapshot object, and slot mapping; its continuation remains blocked.

The recovery result preserves four distinct outcomes:

- `ACKNOWLEDGED`: the selected port contract says recovery work is durable;
- `UNAVAILABLE`: no durable attempt was possible;
- `UNKNOWN`: the persistence acknowledgement itself is ambiguous;
- `REJECTED`: the sink proved it did not accept the work.

Only `ACKNOWLEDGED` sets `recovery_acknowledged`. Exceptions and invalid port
results propagate and are never coerced into durable acceptance.

## Remaining Boundary

This module defines a port contract, not a journal implementation. D3A still
requires product/operations approval of the authoritative Redis or PG backend,
CAS/lease semantics, retention, replay, and degraded mode. The activation gate
must reference that approved contract before runtime wiring.

**P10 R4B-RECOVERY-HANDOFF PASS.** Blocked outcomes now have a mandatory,
typed durable-handoff seam without prematurely selecting a backend.
