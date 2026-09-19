# P10 D2C-COORDINATOR - One-Step Protection Verification

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** The
> coordinator has no runtime caller and performs no direct network, Redis,
> thread, sleep, or trading operation.

## Outcome

`ProtectionVerificationCoordinator.step` composes one injected signed-query
attempt with strict Binance normalization and the verified-ACTIVE commit port.
One call performs at most one query and one commit. Scheduling remains the
caller's responsibility.

The result preserves distinct actions:

- `ACTIVE` only for acknowledged APPLIED/ALREADY_ACTIVE;
- `QUERY_AGAIN` with an explicit timestamp while within both budgets;
- `EXHAUSTED` when attempt/deadline policy closes;
- `RELOAD_CANONICAL` for a three-token `STALE` result;
- `RESOLVE_UNKNOWN` for an ambiguous commit acknowledgement;
- `QUARANTINE` for malformed evidence, identity/spec conflicts, or invalid
  canonical state.

Query exceptions, temporarily missing exchange exposure, and Redis
unavailability use the same attempt/deadline/backoff envelope. The coordinator
does not busy-loop, sleep, silently extend deadlines, or collapse UNKNOWN into
success/failure.

## Transport Boundary

`BinanceVerificationQueryPort` returns already-fetched position-risk and open
Algo payloads with a local completion timestamp. Endpoint choice, signing,
timeouts, HTTP status classification, rate-limit telemetry, and environment
routing remain an injected transport responsibility.

Existing `_light_fapi_get` remains unchanged and is not wired here. Active
runtime use still requires a reviewed signed transport and durable scheduling
or restart recovery. R4B remains `IN_PROGRESS`.

**P10 D2C-COORDINATOR PASS.** The complete query-to-ACTIVE control decision is
testable without I/O and ready for a default-off runtime seam.
