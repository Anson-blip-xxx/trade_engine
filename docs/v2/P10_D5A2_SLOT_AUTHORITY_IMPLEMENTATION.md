# P10-07C / P10-D5A-2 - Dedicated Slot Authority Store And Generation CAS

> Dormant production infrastructure only. **ACTIVE RUNTIME BEHAVIOR = 0 CHANGE.**
> No trading path imports or calls this authority store.

## 1. Scope

P10-07C adds:

- immutable `SlotAuthority` domain records;
- status/provenance/schema enums and validation;
- deterministic JSON serialization;
- typed read and mutation acknowledgements;
- an injected Redis adapter;
- one Lua compare-and-transition primitive;
- initialization, episode allocation, flat transition, migration adoption, and
  reconstructed-quarantine primitives.

It does not decide which live position owns an episode and does not scan, adopt,
reconstruct, enqueue, cancel, create, close, or reconcile anything automatically.

## 2. Production Modules

```text
position_identity/authority.py
position_identity/authority_redis.py
```

`position_identity/__init__.py` exports the new types. No existing business
production module changed.

The domain is stdlib-only. The adapter imports no Redis library and receives
explicit `redis_get` and `redis_eval` callables. Import performs no IO.

## 3. Authority Record

`SlotAuthority` is a frozen value object:

```text
exchange_position_key: ExchangePositionKey
episode_id: string | null
slot_generation: nonnegative integer
status: ACTIVE | FLAT | QUARANTINED
provenance: NATIVE | MIGRATED | RECONSTRUCTED | null
revision: positive integer
legacy_position_id_alias: string | null
created_at: nonnegative finite timestamp
updated_at: nonnegative finite timestamp
schema_version: 1
```

ACTIVE and QUARANTINED require episode and provenance. Initial FLAT has neither;
post-episode FLAT retains both as last-episode lineage.

No API key, secret, private material, wallet, strategy operation payload, queue
item, or journal history is stored.

## 4. Schema And Serialization

`schema_version = 1` is mandatory. Unknown versions, missing/extra fields,
invalid JSON, invalid enums, invalid generation/revision, or slot shape errors
are rejected explicitly.

Serialization is sorted compact JSON with ASCII escaping. Pickle and Python
`repr` are not used. Deserialization reconstructs `ExchangePositionKey` and
revalidates the complete domain record.

## 5. Redis Key Namespace

The adapter accepts only `ExchangePositionKey` and calls its P10-07B
`to_storage_key()`:

```text
pm:slot:v1:<sha256(canonical-exchange-position-key)>
```

It never accepts loose symbol/principal/environment arguments and never reads or
writes `pm:positions`.

## 6. Generation Model

Selected exact model:

```text
initial slot       FLAT generation=0 revision=1
episode A          ACTIVE generation=1 revision=2
episode A closes   FLAT generation=1 revision=3
episode B          ACTIVE generation=2 revision=4
```

Generation increments once per new allocated/adopted/reconstructed episode. It
does not increment for mutation inside one episode, never decrements, and never
resets at FLAT.

The authority key is retained indefinitely with no TTL and no delete operation,
so generation remains the per-slot high-water across restart and reopen.

## 7. Revision Model

Revision is independent from generation:

- every authority mutation increments revision by one;
- optimistic CAS compares exact expected revision;
- generation changes only for a new episode;
- ACTIVE→FLAT changes revision but retains generation;
- revision is not used as episode identity.

## 8. Read API

```text
get_slot_authority(key: ExchangePositionKey) -> AuthorityReadResult
```

Read codes:

- `FOUND`
- `NOT_FOUND`
- `MALFORMED`
- `BACKEND_ERROR`

Malformed data is never reinterpreted or overwritten. A stored record whose
embedded slot differs from the requested key is malformed.

## 9. Atomic Initialization

```text
initialize_flat(key, now)
```

One Lua invocation performs create-if-absent. New state is FLAT generation 0,
revision 1. Existing valid state returns `ALREADY_INITIALIZED` or
`ALREADY_ACTIVE`; malformed state returns `MALFORMED` and is preserved.

## 10. Episode Allocation

```text
allocate_new_episode(
  key,
  candidate_episode_id,
  expected_revision,
  expected_slot_generation,
  expected_episode_id,
  now,
)
```

The caller supplies an opaque candidate ID. Allocation requires exact CAS match
and current FLAT status. Success installs NATIVE ACTIVE state, generation N+1,
and revision R+1.

Concurrent callers using the same expected state cannot both succeed. One Lua
SET wins; every stale candidate receives `CONFLICT` and the winner record.

## 11. ACTIVE To FLAT

```text
transition_active_to_flat(
  key,
  expected_episode_id,
  expected_slot_generation,
  expected_revision,
  now,
)
```

All expected fields must match. Success preserves episode ID, provenance,
legacy alias, generation, and created time while changing status to FLAT and
incrementing revision.

Double flat is `INVALID_STATE` or stale `CONFLICT`, never deletion or reset.

## 12. Controlled Adoption Primitive

```text
adopt_if_unowned(...)
```

This is storage capability only. It does not inspect `pm:positions` or exchange
state. The caller supplies candidate episode ID, legacy alias, and expected FLAT
CAS values. Success creates MIGRATED ACTIVE authority with generation N+1.

Competing adoption candidates have one winner; loser reloads the typed current
record and cannot overwrite it.

## 13. Reconstructed Primitive

```text
create_reconstructed_quarantined(...)
```

Success creates `RECONSTRUCTED` + `QUARANTINED`, never ACTIVE. A normal allocation
cannot silently replace a quarantined owner. Activation policy remains P10-07D
or later work.

## 14. Typed Acknowledgements

Mutation result codes:

- `APPLIED`
- `ALREADY_ACTIVE`
- `ALREADY_INITIALIZED`
- `CONFLICT`
- `NOT_FOUND`
- `INVALID_STATE`
- `MALFORMED`
- `BACKEND_ERROR`

`AuthorityAck` carries the parsed current/new record when available. Redis
exceptions remain `BACKEND_ERROR`; they are not collapsed into conflict or
missing state.

## 15. Lua CAS

`COMPARE_AND_TRANSITION_SLOT_LUA` performs one atomic Redis operation:

1. GET the dedicated key;
2. decode and validate schema/revision/generation/status;
3. compare expected revision, generation, episode, and status;
4. SET the complete new record only when every comparison passes;
5. return a typed code plus current/new raw record.

The script uses no lock, TTL, expiry, delete, file fallback, or positions key.
Existing lock helpers are not correctness dependencies.

## 16. Failure And ABA Guarantees

- malformed/unknown-version records are preserved and reported;
- backend GET/EVAL errors remain distinct;
- stale revision, generation, or episode cannot mutate;
- FLAT retains high-water;
- after A generation 1 closes and B generation 2 opens, delayed A generation 1
  transition receives `CONFLICT` and B remains current;
- no generic bool result hides the cause.

These are local authority guarantees. Exchange effects remain outside this
store and are not wired in P10-07C.

## 17. Dormant Wiring Proof

Architecture guards verify:

- no active runtime production file references `SlotAuthority` or adapter;
- startup still does not require account principal configuration;
- legacy position payload has no episode fields;
- queue and worker remain four-field;
- adapter does not read/write `pm:positions`;
- no Redis/file module is imported;
- no TTL, lock, or file fallback exists.

## 18. Next D5A-3 Integration

P10-07D / D5A-3 is next and `READY_FOR_IMPLEMENTATION`. It may build dormant
controlled-adoption and reconstructed-quarantine orchestration over this store.
It must still avoid active flow wiring until rollout policy and deployment
guards are verified.

## 19. Rollback

`git revert <P10-07C commit>` removes authority infrastructure, tests, docs, and
exports. Since no production caller invokes it, rollback does not read, migrate,
create, or delete Redis authority state and cannot change trading behavior.

## 20. Verification

| Check | Result |
|---|---|
| domain/serialization tests | PASS |
| CAS/concurrency tests | PASS |
| ABA/adoption/reconstructed tests | PASS |
| malformed/backend tests | PASS |
| subprocess import isolation | PASS |
| active runtime callers | none |
| existing business production files modified | none |
| behavior suites | PASS |
| diff check | PASS |

**P10-07C PASS.**

P10-D5A-2 is **IMPLEMENTED / CLOSED**. P10-07D / D5A-3 is next and
`READY_FOR_IMPLEMENTATION`.
