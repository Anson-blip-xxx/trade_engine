# P10 R4B-MIGRATED-DESIRED-DECLARATION

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No runtime
> caller, exchange call, retry loop, or backend selection was added.

`MigratedProtectionDeclarationService` invokes the injected desired-state
`declare` port exactly once and only for a READY migration plan. Exchange
verification is permitted only when APPLIED/ALREADY_APPLIED returns a durable
record exactly equal to the planned desired record. Same-spec but non-exact
records quarantine rather than silently losing the legacy Algo alias.

STALE/CONFLICT require reload, UNAVAILABLE remains retryable, UNKNOWN requires
same-effect acknowledgement resolution, and malformed/invalid outcomes never
advance. The service contains no implicit retry or exception swallowing.

**P10 R4B-MIGRATED-DESIRED-DECLARATION PASS.** Runtime wiring remains blocked
until durable recovery and current exchange verification are composed.
