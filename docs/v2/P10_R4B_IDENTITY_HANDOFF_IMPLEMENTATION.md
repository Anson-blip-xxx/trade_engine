# P10 R4B-IDENTITY - Controlled Legacy Identity Handoff

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** This is not
> the R4B active producer cutover. No startup/runtime caller is wired, and V2
> remains non-deployable.

## Outcome

`RedisLegacyMigrationHandoffAdapter` atomically creates the two identity records
required to describe one trustworthy legacy exposure:

- ACTIVE `MIGRATED` slot authority with a preserved legacy `position_id` alias;
- revision-1 canonical live-position projection for the same episode and slot
  generation.

The adapter uses the existing D5A controlled-adoption classifier and R1 legacy
projection validator. It does not infer identity from symbol, entry price, or
observation time, and it never activates reconstructed exposure.

## Required Evidence

The caller must provide a canonical slot, opaque candidate UUID, explicit
legacy `position_id`, local symbol/side/quantity, exchange side/quantity, a
bounded quantity tolerance, mixed-version status, initialization permission,
operation ID, and timestamp.

Adoption is permitted only when:

- local and exchange symbol/side/quantity evidence agrees;
- mixed-version execution is explicitly false;
- authority is absent with explicit initialization permission, or is FLAT;
- canonical projection is absent;
- the legacy row contains all required projection fields and no canonical
  identity fields.

Missing aliases, mismatch, mixed version, conflicting owner, malformed state,
and backend unavailability cause zero writes. An existing NATIVE owner is
returned as `EXISTING_OWNER`, never converted to MIGRATED.

## Atomicity And Retry

The Lua CAS compares the exact pre-read authority and projection payloads before
writing both proposed records in one Redis operation. A competing mutation
returns `CONFLICT` without partial state. Exact replay is `ALREADY_APPLIED`.

If acknowledgement is lost after Redis execution, the caller receives
`UNKNOWN`; a later retry recognizes the already-adopted legacy alias and returns
the durable episode rather than allocating a new one.

## Why Runtime Wiring Is Deferred

Two gates remain before R4B can become an active producer:

1. the legacy whole-snapshot writer guard must prevent canonical and legacy
   authorities from diverging after adoption;
2. existing exchange protection must be characterized and verified before the
   system can safely mark a desired record ACTIVE or enqueue a replacement.

The current `migrate_existing_positions` only copies `state:s6/s8` dictionaries
and has neither guarantee. Wiring this adapter there now could duplicate a stop
or create two writable sources of position state. Therefore this node stays
dormant and makes no Binance, Redis, database, service, or trading change.

## QA Contract

Isolated-Redis tests cover atomic fresh adoption, exact retry with a different
candidate UUID, side/quantity mismatch, mixed-version quarantine, missing alias,
explicit initialization permission, pre-attempt backend failure, and ambiguous
post-commit acknowledgement recovery.

**P10 R4B-IDENTITY PASS.** R4B active producer remains `IN_PROGRESS` pending
writer fencing and protection-state verification.
