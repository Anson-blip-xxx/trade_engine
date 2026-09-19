# P10 R1 - Canonical Live Position Projection And Revision

> **IMPLEMENTED / CLOSED as dormant V2 infrastructure.**
> **ACTIVE RUNTIME BEHAVIOR = 0 CHANGE.**

## 1. Outcome

R1 adds a versioned, per-physical-slot operational projection with atomic Redis
create/CAS, immutable episode identity, monotonic projection revision, operation
idempotency, controlled legacy compatibility, and a mixed-version writer guard.

The implementation is intentionally not connected to open, close, protection,
monitor, reconcile, startup, or worker paths. It does not read or write the
legacy `pm:positions` snapshot. It does not perform migration automatically.

## 2. Production Modules

```text
position_identity/projection.py
position_identity/projection_redis.py
position_identity/projection_migration.py
```

`position_identity/__init__.py` exports the dormant API. The Redis adapter is
dependency-injected and imports no Redis client. Importing these modules performs
no IO and starts no worker.

## 3. Authority Boundaries

- Binance/exchange remains physical exposure truth.
- `pm:slot:v1:*` remains episode ownership and slot-generation authority.
- `pm:position-projection:v1:*` is the canonical V2 operational projection.
- `pm:positions` remains the active legacy runtime snapshot until R4 cutover; it
  is not upgraded to authority and is never used by the R1 CAS adapter.
- D2A, not this record, will own desired protection generation.
- Exchange order/algo IDs remain aliases, never episode identity.

## 4. Versioned Record

`LivePositionProjection` is frozen and uses `schema_version = 1`:

```text
exchange_position_key
episode_id
slot_generation
identity_provenance
state_revision
last_operation_id
side
system
quantity
entry_price
opened_at
updated_at
legacy_position_id_alias
schema_version
```

The strict parser rejects missing fields, extra fields, unsupported versions,
invalid enums/UUIDs, non-positive quantity/price/revision/generation, invalid
timestamps, and malformed embedded slot identity.

Episode ID, slot generation, provenance, slot key, side, system, opened time,
and legacy alias are immutable inside one projection episode. A normal revision
may update operational values such as quantity or entry price and increments
`state_revision` by exactly one.

No credential, API secret, wallet material, raw webhook, or journal payload is
stored.

## 5. Redis Namespace And Persistence

One key exists per canonical `ExchangePositionKey`:

```text
pm:position-projection:v1:<sha256(canonical-slot-key)>
```

Records have no TTL and no delete primitive in R1. The adapter has no file
fallback, snapshot fallback, or double-write to `pm:positions`.

## 6. Atomic Create And CAS

One Lua invocation performs GET, complete stored-record validation, comparison,
and SET. Create requires revision 1. CAS compares:

```text
expected episode_id
expected slot_generation
expected state_revision
last_operation_id
immutable projection identity
```

The proposed revision must equal current revision plus one. Episode/generation
ABA and stale revisions return `STALE` without mutation. The same operation and
identical canonical JSON return `ALREADY_APPLIED`; reuse of an operation ID with
different content returns `CONFLICT`.

Lua validates every schema field and the embedded slot before overwriting. A
malformed, partial, unknown-field, or unsupported record returns `MALFORMED` and
is preserved.

## 7. Typed Results And Failure Semantics

Read codes are:

- `FOUND`
- `NOT_FOUND`
- `MALFORMED`
- `UNAVAILABLE`

Write codes are:

- `APPLIED`
- `ALREADY_APPLIED`
- `STALE`
- `CONFLICT`
- `NOT_FOUND`
- `MALFORMED`
- `INVALID`
- `UNAVAILABLE`
- `UNKNOWN`

A failed injected pre-attempt availability probe is `UNAVAILABLE`, proving Lua
was not invoked. An exception from the Lua call is `UNKNOWN`, because the server
may have committed before acknowledgement was lost. Neither is success and
neither permits fallback to the legacy snapshot.

## 8. Controlled Legacy Compatibility

`prepare_legacy_projection` is a pure, dormant adapter. It produces a revision-1
projection only when all of the following are already true:

1. D5A controlled adoption produced ACTIVE `MIGRATED` authority;
2. slot, episode, generation, legacy alias, symbol, side, quantity, entry, and
   timestamps validate;
3. the canonical slot is absent;
4. the legacy row contains no canonical/mixed-version fields.

Unadopted, reconstructed, malformed, mismatched, mixed-version, backend-
unavailable, or malformed-canonical cases return quarantine decisions. This
module never allocates authority and never activates reconstructed exposure.

An exact existing migrated projection is idempotently `ALREADY_PROJECTED`.
Conflicting canonical lineage is quarantined.

## 9. Mixed-Version Writer Guard

`legacy_snapshot_write_decision` is the R1 compatibility guard:

- canonical `NOT_FOUND` -> `ALLOW_WHILE_UNMIGRATED`;
- canonical `FOUND` -> `BLOCK_CANONICAL_PRESENT`;
- malformed, unavailable, or invalid read -> `FAIL_CLOSED`.

The guard is delivered and tested but remains dormant. R4 must invoke it at the
legacy writer boundary before any active migration. R4 may not enable canonical
production writers while an unguarded whole-snapshot writer can still mutate the
same slot.

## 10. QA Evidence

Automated R1 tests cover:

- frozen model, exact schema, deterministic round trip, validation, and immutable
  identity;
- create, exact retry, altered retry, revision CAS, stale writer, episode/
  generation ABA, and two-writer concurrency;
- pre-attempt `UNAVAILABLE` versus ambiguous post-attempt `UNKNOWN`;
- invalid Redis responses and import-time zero IO;
- malformed/partial Redis records preserved without overwrite;
- controlled legacy projection, reconstructed/unadopted quarantine, alias/slot
  mismatch, mixed fields, existing canonical lineage, and fail-closed writer
  decisions;
- real Lua execution against a disposable Redis process using a temporary Unix
  socket, TCP disabled, persistence disabled, and an isolated database;
- Phase 10 architecture facts and active-runtime non-wiring.

Repository gates also include `tests/phase10`, `tests/position_manager`,
`tests/execution`, full pytest, Ruff for all new files, and `git diff --check`.

## 11. Rollback And Deployment

There is nothing to deploy for R1 alone. Rollback is removal of the dormant
modules/tests/docs before R4. No service restart, database migration, live Redis
key creation, exchange call, or trading action is part of this ticket.

R1 completion unblocks only the projection prerequisite. P10-D3D-1D remains
blocked by R2/P10-D2A durable desired-protection generation and must not be wired
or deployed from this branch.

**P10 R1 PASS.**
