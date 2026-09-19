# P10 R4B-MUTATION - Authority-Fenced Projection Reduction

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No lifecycle
> caller is wired. This node supplies the canonical quantity-reduction CAS that
> migrated partial-close handling requires.

## Outcome

`RedisProjectionMutationAdapter.reduce_quantity` updates one canonical live
projection only while the exact ACTIVE authority remains current. The operation
requires expected:

- exchange-position slot;
- episode ID;
- slot generation;
- authority revision;
- projection revision;
- strictly smaller positive quantity;
- stable operation ID and timestamp.

The proposed projection retains slot, episode, generation, provenance, legacy
alias, side, system, entry and open time. Only quantity, projection revision,
last operation ID and update time change.

## Atomic Fence

The adapter strictly parses authority and projection before mutation and checks
their slot, episode, generation, provenance and legacy alias relationship. Its
two-key Lua operation compares the exact pre-read authority and projection
payloads, then writes only the revised projection.

This closes the race where a partial-close update is prepared while ACTIVE but
the slot becomes FLAT or a newer episode is allocated before persistence. Any
authority or projection change returns `STALE` and performs no write.

## Retry And Failure Semantics

- exact replay with the original expected revision and same operation ID/target
  quantity returns `ALREADY_APPLIED` while authority remains the same ACTIVE
  generation;
- a different operation using an old projection revision returns `STALE`;
- missing records return `NOT_FOUND`;
- malformed records return `MALFORMED`;
- pre-attempt backend failure returns `UNAVAILABLE`;
- an exception after Lua may have committed and returns `UNKNOWN`;
- a later exact retry resolves an ambiguous committed write as
  `ALREADY_APPLIED`;
- zero, negative, unchanged, or increased quantity is `INVALID`.

An exact retry after authority becomes FLAT is deliberately `STALE`, not
success: no mutation may claim authority over a closed slot.

## Runtime Gate

This adapter is not yet called from `PositionLifecycleService.partial_close`.
Runtime wiring must capture canonical identity before the exchange order, use a
stable close operation ID derived from acknowledged execution identity, require
this CAS before reporting durable success, and propagate typed failure instead
of relying on the legacy `PositionStateService.save` swallow-on-error contract.

No service, trading endpoint, production Redis, or database was touched.

## QA Contract

Isolated-Redis tests cover successful reduction, exact retry, different stale
operation, FLAT authority, non-reduction inputs, authority change between read
and Lua, ambiguous post-commit acknowledgement recovery, missing records, and
backend failure.

**P10 R4B-MUTATION PASS.** The mutation primitive is ready; active lifecycle
wiring and typed legacy save acknowledgement remain `IN_PROGRESS`.
