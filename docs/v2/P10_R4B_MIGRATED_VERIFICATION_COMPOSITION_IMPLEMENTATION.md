# P10 R4B-MIGRATED-VERIFICATION-COMPOSITION

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No runtime
> caller, loop, scheduler, endpoint, credential, or direct I/O was added.

`MigratedProtectionVerificationService.verify_once` calls the existing bounded
coordinator exactly once and only after an exact durable desired declaration
ACK. The slot is derived from that desired record rather than accepted from a
second caller input. The coordinator then re-reads canonical durable state,
queries current exchange evidence through its injected transport, and performs
the verified-ACTIVE three-token CAS.

All coordinator outcomes remain distinct. Only `ACTIVE` sets the composed
result active; QUERY_AGAIN, EXHAUSTED, QUARANTINE, RELOAD_CANONICAL, and
RESOLVE_UNKNOWN are returned unchanged. Non-acknowledged declaration outcomes
perform zero exchange queries.

**P10 R4B-MIGRATED-VERIFICATION-COMPOSITION PASS.** Durable scheduling and the
default-off activation gate still block production runtime wiring.
