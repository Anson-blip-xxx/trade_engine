# P10-06B / P10-D3D-1 - Protection Queue Episode Fence Authority Contract

> Architecture and semantic contract only. Production code is unchanged. This
> document defines what authority a future protection worker must prove before
> it may cancel, create, or write protection state.

## 1. Scope And Invariant

P10-06A proved that an old four-field protection task can cancel protection,
create a stale stop, and write its `algoId` into a reopened same-symbol episode.
P10-06B defines the identity authority required before changing that queue.

The invariant is:

```text
A protection worker may mutate exchange or local protection state only when it
proves that its task belongs to the current episode occupying the exact exchange
position slot and, where applicable, the current desired protection generation.
```

This document does not authorize:

- changing `_ALGO_QUEUE`, worker, placement, state, Redis, or marker schemas;
- adding an episode or generation production field;
- changing open, close, reconcile, migration, or protection behavior;
- using current `position_id` as canonical episode authority;
- implementing a journal, replay, migration, or CAS.

## 2. Current Supported Exchange Model

The implementation supports one configured Binance USD-M futures principal and
one selected environment per process. State and exchange projections collapse
that context to a symbol-keyed dictionary.

Current order and state evidence establishes **one-way mode semantics**:

- active admission rejects any nonzero same-symbol exposure regardless of side;
- REST/WS derive LONG or SHORT from signed aggregate `positionAmt`;
- PM open, full close, partial close, and Algo protection use
  `positionSide='BOTH'`;
- active shared-executor open omits `positionSide`, relying on account one-way
  behavior;
- REST/WS would overwrite two hedge rows because both are keyed by symbol;
- no account position-mode detection or hedge-side reconcile exists.

Therefore current supported scope is:

```text
one configured account/subaccount principal
+ one prod/demo environment
+ BINANCE USD_M_FUTURES
+ position_mode = ONE_WAY
+ symbol
+ slot_side = BOTH
```

Hedge mode is not supported merely because Binance payloads contain a
`positionSide` concept.

## 3. Canonical Exchange Position Slot

Future canonical key:

```text
exchange_position_key = (
  exchange,              # BINANCE
  product,               # USD_M_FUTURES
  environment,           # PROD or DEMO endpoint domain
  account_principal_id,  # stable account/subaccount identity, not API key
  position_mode,         # ONE_WAY or future HEDGE
  symbol,
  slot_side,             # BOTH for ONE_WAY; LONG/SHORT for future HEDGE
)
```

Required properties:

- stable across process restart and API credential rotation;
- explicit enough that two accounts or prod/demo cannot collide in one store;
- normalized so every producer and worker computes the same value;
- immutable for the lifetime of one physical exchange slot definition.

Excluded dimensions:

| Dimension | Why excluded |
|---|---|
| strategy/system | local ownership policy, not broker slot identity |
| economic LONG/SHORT in one-way mode | mutable signed exposure in the same `BOTH` slot |
| margin mode | mutable crossed/isolated configuration, not an independent slot |
| API key/secret | authorization credential; rotation must not create a slot |
| episode ID | occupant identity, not reusable slot identity |
| order/fill/algo ID | effect aliases, not slot identity |

A subaccount is represented by `account_principal_id`. Current code selects it
only implicitly through one credential pair and does not persist that identity.

## 4. Position Episode Definition

This contract adopts D5:

**A position episode is one continuous lifecycle in one exchange position slot,
beginning when confirmed physical exposure transitions from flat to nonzero and
ending when exchange evidence confirms that slot is flat again.**

| Event | Episode effect |
|---|---|
| first confirmed nonzero exposure from flat | activate a new episode |
| partial fill while opening from flat | activate one episode at first nonzero fill |
| later fill without crossing flat | remain in the same episode |
| intentional scale-in, if later allowed | remain in the same episode |
| partial close with nonzero remainder | remain in the same episode |
| stop move or protection replacement | same episode; advance protection generation |
| full close confirmed flat | end the episode physically |
| reopen after confirmed flat | activate a different episode and slot generation |
| direct sign flip | old episode end plus new episode start; lineage requires order/fill evidence |

Request, order, fill, strategy, protection, and episode identities remain
separate even where the simplest open path is temporarily one-to-one.

## 5. Canonical Episode Token Requirements

`episode_id` must be:

1. opaque and immutable;
2. created at most once for one activated episode;
3. durably bound to the current slot before asynchronous protection handoff;
4. readable from the worker's authority source;
5. different for every reopen after confirmed flat;
6. restart-stable for the promised recovery window;
7. locally assigned, not dependent solely on one exchange order/fill alias;
8. independent of mutable entry, quantity, side, system, and wall-clock
   fingerprint;
9. schema-versioned with native/migrated/reconstructed provenance;
10. usable as an expected value in CAS and fenced ownership checks;
11. preserved in history after the slot moves to a later episode;
12. separate from `slot_generation` and `protection_generation`.

The token identifies lineage. A generation orders ownership changes. Both are
needed because a UUID alone cannot tell whether a stale owner still has mutation
rights, and a counter alone is a poor portable historical identity.

## 6. Token Candidate Comparison

| Candidate | Pre-async | Retry/restart | Reopen | Scale-in | Reconstruction/migration | Ruling |
|---|---:|---:|---:|---:|---|---|
| A. current `position_id` | active path yes | best-effort only | normally differs | can remain same if preserved | mixed formats; cannot recover from exchange | `TEMPORARY_FENCE_ONLY` |
| B. UUID at confirmed episode creation | yes before enqueue | stable if durable binding ACKs | always different | later requests attach to same UUID | supports explicit provenance/alias | recommended episode ID |
| C. request-derived token | pre-order if D1 exists | request-stable | differs by request | incorrectly creates one episode per scale-in request | failed request can create no episode | alias only |
| D. exchange order ID | after ACK | exchange-stable if queryable | order-specific | many orders per episode | ambiguous ACK/multiple orders | alias only |
| E. first-fill ID | after first fill | query-contract dependent | can anchor transition | later fills need aliases | runtime fill ID absent today | alias/activation evidence only |
| F. per-slot monotonic generation | after durable allocation | strong if atomic/durable | increments | unchanged within episode | needs one slot allocator | required fence, not sole ID |
| G. opaque UUID + slot generation | before handoff | strong | UUID and generation change | both remain for same episode | supports native/migrated/reconstructed | **recommended** |

No deterministic combination of symbol, system, entry, and local time provides
the same correctness properties.

## 7. Current position_id Ruling

Final ruling:

```text
current position_id = TEMPORARY_FENCE_ONLY
current position_id != canonical episode_id
```

Reasons:

- active S6/S8 creates it after order submission and fill classification;
- it depends on system, symbol, mutable entry, and local wall time;
- retrying a successful effect can generate another value;
- Redis/file persistence is best-effort and unacknowledged;
- legacy lifecycle open normally creates no `position_id` before enqueue;
- merge fallback and PM fallback use incompatible formats;
- exchange snapshots cannot reconstruct the native value;
- it omits account, environment, position mode, and slot authority;
- it is already identity-critical in ledgers and partial-close keys, increasing
  destructive migration risk;
- equality cannot order protection replacements inside one episode.

It may be preserved as `legacy_position_id` and optionally checked as
defense-in-depth. It must not be copied into `episode_id`, used to allocate slot
generation, or accepted as proof of canonical ownership.

## 8. Recommended Canonical Model

```text
exchange_position_key
  -> current_slot_generation
  -> current_episode_id
       -> provenance
       -> legacy aliases
       -> owner policy/allocation
       -> current protection_generation
       -> order/fill/algo aliases
```

The minimum authority tuple carried by protection work is:

```text
(exchange_position_key, episode_id, expected_slot_generation,
 expected_protection_generation)
```

`episode_id` distinguishes A from B. `slot_generation` proves current slot
ownership. `protection_generation` distinguishes desired stop revisions inside
one episode.

## 9. Episode Creation Timing

| Timing | Phantom/retry behavior | Crash/ambiguity behavior | Assessment |
|---|---|---|---|
| A. request reservation | can allocate unused candidate | best pre-effect correlation if D1 durable request exists | candidate allocation is allowed; not episode activation |
| B. order ACK | ACK need not mean fill/nonzero | ambiguous ACK can create false episode | rejected as activation authority |
| C. first confirmed nonzero fill/exposure | matches flat-to-nonzero semantics | crash gap needs reconstruction/operation evidence | **activation authority** |
| D. local projection creation | available before enqueue | local write may lag or fail after physical effect | binding step, not physical creation authority |

Recommended rule:

1. An opaque candidate UUID may be reserved before submission when a durable D1
   operation exists.
2. Reservation does not mean an episode exists.
3. The candidate is activated only when exchange evidence confirms the slot
   transitioned from flat to nonzero.
4. Without a reserved candidate, allocate the UUID at that confirmed transition.
5. Atomically increment slot generation and durably bind slot, episode, and
   provenance.
6. Acknowledge that binding before any asynchronous protection enqueue.
7. Partial first fill activates one episode; later fills attach to it.
8. Ambiguous ACK remains unresolved until exchange evidence proves nonzero
   exposure or a matching fill.

This separates token allocation from episode authority and avoids phantom
episodes for rejected/unfilled requests.

## 10. Episode Creation Authority

No single local caller may declare a live episode merely because it sent a
request or received an order ID.

Minimum activation authority is:

```text
confirmed exchange nonzero exposure/fill for the explicit slot
+ durable CAS binding of episode_id and next slot_generation
+ identity provenance and known aliases
```

Exchange evidence owns physical existence. The durable slot binding owns local
lineage and mutation authority. If physical exposure exists but the binding is
not acknowledged, the slot is `UNATTRIBUTED/RECOVERY_REQUIRED`, not safely
eligible for an unfenced protection task.

The operational position-state authority owns the current slot binding. The
queue, PG event ledger, closed marker, logs, and `algo_sl_id` do not.

## 11. Episode End Authority

Physical episode end authority is:

```text
exchange-confirmed flat for the exact exchange_position_key
```

Local close success, function return, marker write, ledger insert, or state pop
does not make exchange exposure flat.

After flat confirmation:

- slot authority closes the current episode under expected generation;
- current ownership is no longer valid for new mutations;
- protection work for that episode becomes stale immediately;
- local accounting, tombstone, marker, and projection finalization may lag;
- the episode ID and generation are never reused;
- the next nonzero transition increments generation and receives a new ID.

## 12. Slot Ownership Primitive

One normalized `exchange_position_key` has at most one current active episode
owner in the present one-way model.

Conceptual current-owner record:

```text
exchange_position_key
current_episode_id
slot_generation
status = ACTIVE | FLAT | RECOVERY_REQUIRED | QUARANTINED
revision
owner_fencing_token
```

Every participating local open, close, protection, monitor, ghost, and reconcile
mutation must eventually compare expected ownership. D3D-1 implements only the
protection boundary, but its guarantee depends on the slot authority not being
silently replaced by an unfenced writer.

Intentional scale-in, if approved later, attaches new request/order/fill aliases
to the current episode while exposure never crosses flat. It does not allocate a
new current owner.

## 13. Slot Generation

`slot_generation` is a durable monotonic integer scoped to one
`exchange_position_key`:

```text
generation 40 = FLAT
generation 41 = episode A ACTIVE
generation 42 = episode A CLOSED/slot transition
generation 43 = episode B ACTIVE
```

The exact increment convention may use one increment per ownership transition
or one per activated episode. It must satisfy:

- B's generation is strictly greater than A's;
- generation allocation is atomic and never rolls back/reuses a number;
- every current-owner write is CAS-protected by expected revision/generation;
- restart and failover read the same durable current value;
- stale worker generation cannot mutate current local state;
- generation is not derived from time or UUID ordering.

For a simpler implementation, one increment per activated episode is sufficient
if closing atomically marks that generation non-active before reopen. The state
machine and comparison semantics must be fixed before implementation.

## 14. Protection Generation

`protection_generation` is monotonic within one episode:

```text
episode B, protection 1 = initial stop
episode B, protection 2 = break-even stop
episode B, protection 3 = trailing stop
```

Rules:

- retry of the same desired spec retains generation;
- trigger or approved quantity/spec change increments generation;
- old generation cannot cancel, create, or write back over current generation;
- close invalidates all generations of the ended episode;
- generation is durable desired state, not queue position;
- it is separate from slot generation and state revision.

D3D-1's A-versus-B episode fence can be implemented before full replacement
policy, but replacement producers cannot claim complete stale-work safety until
D3D-2 defines and enforces this generation.

## 15. Future State Schema

Conceptual minimum, not an implementation:

```text
schema_version
state_revision
exchange_position_key {
  exchange
  product
  environment
  account_principal_id
  position_mode
  symbol
  slot_side
}
episode_id
slot_generation
episode_status
identity_provenance = NATIVE | MIGRATED | RECONSTRUCTED
legacy_position_id (nullable alias)
origin_request_id (nullable alias)
protection_generation
desired_protection_spec
algo_sl_id (nullable exchange alias)
system / ownership metadata
entry / qty / side / margin / strategy fields
```

Must-have for D3D-1A is the explicit slot key, immutable episode ID, monotonic
slot generation, status, provenance, legacy alias, schema/revision, and an
acknowledged CAS authority. Protection desired-state fields can follow D3D-2 if
initial queue fencing is staged separately.

## 16. Future Queue Contract

Tuple candidate:

```text
(
  symbol,
  side,
  trigger_price,
  qty,
  exchange_position_key,
  episode_id,
  slot_generation,
  protection_generation,
)
```

A frozen dataclass/value object is preferable once behavior implementation is
approved because eight positional fields are easy to misorder. Either form must
be immutable and validated. Queue shape is not changed in P10-06B.

The queued `symbol` is an exchange parameter and diagnostic convenience. The
full slot key is the ownership lookup key.

## 17. Worker Validation Contract

Before acquiring mutation permission:

1. parse and validate the complete task identity;
2. load the authoritative current slot owner;
3. require readable state and `ACTIVE` status;
4. require exact exchange-position-key equality;
5. require exact episode-ID equality;
6. require exact slot-generation equality;
7. require exact desired protection generation where D3D-2 applies;
8. acquire/validate the slot mutation claim bound to those expected values;
9. only then call exchange cancel/create;
10. condition writeback on the same expected values and state revision.

Any missing, malformed, unavailable, inactive, or mismatched value yields
`STALE/UNVERIFIABLE` and no exchange mutation.

## 18. Triple Validation And Mutation Ownership

Three validation points are required:

| Point | Timing | Purpose |
|---|---|---|
| V1 | before cancel | prevent stale work from removing current protection |
| V2 | after cancel/query and immediately before create | stop if ownership or desired generation changed during exchange IO |
| V3 | before `algo_sl_id`/state writeback | prevent old ACK from polluting current state |

V1/V2 are not three unlocked reads. The worker must hold or renew a mutation
claim bound to expected slot/episode generation, and all cooperating lifecycle
transitions must respect the same authority. A lease without a fencing value is
insufficient.

Binance does not accept the local generation as a conditional mutation token.
Therefore a process paused after validation remains a residual remote-side risk.
The implementation must minimize this window, revalidate immediately before
each call, treat lease loss as no further mutation permission, and reconcile
ambiguous exchange effects. Absolute exchange atomicity belongs to D3C/D3A;
D3D-1 prevents known stale queued work from being admitted by current local
authority.

## 19. Conditional Writeback

Current symbol-only writeback is prohibited in the future contract:

```text
positions[symbol]['algo_sl_id'] = returned_id
```

Required conceptual CAS:

```text
update current protection alias
where exchange_position_key = expected_slot
  and episode_id = expected_episode
  and slot_generation = expected_slot_generation
  and protection_generation = expected_protection_generation
  and state_revision = expected_revision
```

CAS outcomes must distinguish applied, already applied, stale mismatch,
unavailable, and unknown acknowledgement. A stale result is not retried against
whatever episode currently owns the symbol.

## 20. Legacy Active-Position Options

Existing active rows may have only legacy `position_id`, or none, and no slot or
protection generation.

| Option | Safety | Availability | Assessment |
|---|---|---|---|
| A. prohibit protection update until flat | strongest without migration | existing episode may retain stale/no protection | safe quarantine fallback |
| B. lazy assign on ordinary read | unsafe without atomic single assignment | convenient | rejected as plain read-side behavior |
| C. controlled one-time adoption | safe with exchange check, exclusive slot CAS, provenance, alias | preserves automation | recommended when product approves adoption |
| D. deterministic ID from legacy `position_id` | repeatable but inherits collision/split semantics | easy | rejected as canonical identity |
| E. use current `position_id` directly | no new lineage authority | highest compatibility | rejected; alias only |
| F. reconstruct after restart | safe only with new opaque ID/provenance and quarantine rules | restores operability | valid `RECONSTRUCTED`, not original lineage |

Recommended rollout policy:

1. New confirmed episodes receive native authority.
2. Existing active positions require an explicit controlled adoption step under
   exclusive slot CAS, preserving the old ID as alias and marking `MIGRATED`.
3. If adoption cannot prove one current slot owner, quarantine protection
   mutation until flat/operator resolution.
4. Do not silently assign IDs during any ordinary load/merge.

Product/operations must approve whether controlled adoption is automatic during
maintenance or legacy positions remain quarantined until flat. This is a
specific D5A blocker, not a generic full migration dependency.

## 21. Legacy Queue Item Contract

P10-06A's recommendation becomes a migration contract:

```text
old four-tuple = LEGACY_UNFENCED
LEGACY_UNFENCED = DROP before cancel/create/writeback
```

A compatibility parser may identify and log the old shape. It may not derive
the current episode at dequeue time, because that would relabel A's stale
trigger/quantity as B's desired work.

## 22. Deployment Strategy

Normal process restart clears the memory-only queue, so persisted queue-payload
migration is unnecessary. That fact does not solve active-position migration.

Deployment dimensions:

| Concern | Required treatment |
|---|---|
| old in-memory four-tuples | restart clears them; hot reload must drain/drop |
| existing active positions | controlled adoption or quarantine decision |
| new opens | assign native episode authority before enqueue |
| reconstructed exchange exposure | assign explicit provenance under slot CAS or quarantine |
| mixed-version processes | prohibited from sharing mutation authority unless old workers are stopped |
| rollback | new state schema/aliases must remain readable or deployment must block unsafe rollback |

No migration is implemented here.

## 23. Legacy Alias Model

Canonical identity is additive:

```text
episode_id = new immutable authority
legacy_position_id = preserved alias
```

Existing ledger primary keys, events, partial-close hashes, reports, and external
consumers continue to reference the old value until an approved alias-aware
migration exists. Do not rewrite those values in place.

An alias record should include alias type, value, provenance, episode ID,
creation source, and validity/retention metadata. Alias equality does not grant
current slot mutation permission.

## 24. Reconstructed Episode

When exchange exposure is nonzero but native lineage is unavailable:

1. exchange evidence proves physical exposure, not original identity;
2. acquire exclusive authority for the explicit slot;
3. ensure no valid current durable owner can be recovered;
4. allocate a new opaque episode ID and next slot generation;
5. mark provenance `RECONSTRUCTED` and retain observed/legacy aliases;
6. apply product policy: automatic adoption, quarantine, or operator approval;
7. only enqueue protection after the reconstructed owner is durably active.

The reconstructed ID must not pretend to be the original native episode ID.
Unknown old work can never prove membership in the reconstructed episode and is
dropped.

## 25. Scale-In Relation

The fence contract does not decide whether scale-in is allowed. It only defines
identity if product later allows it:

```text
nonzero exposure before request
+ no confirmed flat transition
=> same episode_id and slot_generation
=> new request/order/fill aliases
=> protection generation may advance for quantity/spec change
```

Therefore scale-in policy is **not a D3D-1 blocker**. The implementation may
continue current no-scale-in admission while using an episode model that does
not incorrectly allocate one episode per order.

## 26. Cross-System Ownership Relation

Current S6/S8 share one symbol slot and one metadata `system` value. Future
product policy may allow exclusive ownership, transfer, or allocation sets.

Rules independent of that decision:

- `system` is not part of `episode_id` or `exchange_position_key`;
- ownership metadata is attached to and versioned within an episode;
- changing strategy ownership does not create a new episode while exposure
  remains continuously nonzero;
- every system mutating protection must still prove the same episode/generation.

Cross-system allocation policy is **not a D3D-1 blocker** for stale episode
suppression.

## 27. Marker Relation

A future episode-scoped marker needs at least:

```text
exchange_position_key
episode_id
slot_generation
marker_type
operation_token/generation
```

Compare-and-clear must reject old A when B/current marker differs. Marker work is
D3D-3 and is not implemented or required to change the protection queue in
D3D-1, though both consume the same D5A slot authority.

## 28. Close And Reconcile Relation

Future close operations bind expected `exchange_position_key`, `episode_id`, and
`slot_generation`. A delayed close result for A must not pop, account as, or
write state for B.

Future monitor/reconcile projection writes additionally compare state revision
and current ownership. Exchange snapshots can confirm physical quantity/side
but cannot recover native episode lineage. D3D-4 owns that version/CAS work.

D3D-1 remains protection-only. It must not be expanded into these paths during
its first implementation.

## 29. Fail-Safe And Risk Trade-Off

For a future protection worker, any of the following denies all mutation
permission:

- missing position or episode;
- missing slot/protection generation;
- unreadable authority state;
- slot, episode, or generation mismatch;
- inactive/closed/quarantined episode;
- malformed identity;
- legacy unfenced item;
- lost/expired mutation claim.

Denied permission means:

```text
NO CANCEL
NO CREATE
NO WRITEBACK
DROP + structured local log/metric
```

Drop can leave the current episode temporarily unprotected. Blind execution can
cancel or overwrite the correct protection of another episode. The fence layer
therefore prioritizes identity correctness. D2 owns health/admission, desired-
state retry, quarantine, open halt, operator escalation, or compensation-close
policy. D3D-1 does not market-close.

## 30. Authority Decision Questions

| Question | Contract answer/status |
|---|---|
| Q1 opaque UUID or generation-derived ID? | opaque UUID plus separate monotonic slot generation |
| Q2 legacy active lazy migration? | plain lazy read assignment rejected; choose controlled adoption or quarantine |
| Q3 reconstructed exposure auto episode? | permitted only under slot CAS with `RECONSTRUCTED` provenance; auto/operator policy remains explicit |
| Q4 slot generation durable monotonic? | yes, mandatory |
| Q5 scale-in allowed? | product decision, but not a D3D-1 blocker; if allowed it remains same episode |
| Q6 systems share ownership? | product decision, but ownership is separate and not a D3D-1 identity blocker |
| Q7 account principal source? | must be stable/configured or exchange-resolved; raw API key is forbidden |
| Q8 legacy adoption mode? | product/operations must choose maintenance adoption versus quarantine-until-flat |

## 31. True Blockers And Dependency Minimization

### True D3D-1 blockers

1. D5A normalized slot namespace, including stable account principal source.
2. D5A durable opaque episode ID and monotonic slot-generation authority with
   acknowledged CAS.
3. D5A activation/end transitions and native/migrated/reconstructed provenance.
4. Explicit legacy active-position adoption versus quarantine policy.
5. A slot mutation claim/serialization contract respected by supported local
   lifecycle writers around cancel/create.
6. Every supported protection enqueue producer must have authority before it
   can emit a fenced item.

### Not blockers for the narrow A-versus-B fence

- approving scale-in behavior;
- deciding cross-system ownership allocation;
- complete historical PG/ClickHouse migration;
- D1 request idempotency implementation;
- durable queue, journal, or restart replay;
- marker redesign;
- close/ghost/monitor/reconcile fencing;
- final D2 replacement gap/overlap policy.

Protection generation remains required for complete same-episode replacement
safety, but D3D-1 can first prevent old episode A from being admitted against B.

## 32. Minimal Authority Contract

The minimum implementable contract is:

1. One normalized current slot has at most one active episode owner.
2. Every position eligible to enqueue protected work has an immutable opaque
   episode ID and monotonic slot generation.
3. Confirmed reopen after flat creates a different ID and greater generation.
4. Slot binding is durably acknowledged before async handoff.
5. Protection tasks carry the complete expected slot/episode authority.
6. Worker obtains mutation permission and validates before cancel and create.
7. Writeback uses expected episode/generation/revision CAS.
8. Unknown, reconstructed-pending, legacy, or malformed work fails closed.
9. Protection generation is a separate monotonic desired-state dimension.
10. Existing legacy IDs remain aliases, never canonical authority.

This contract is independent of whether scale-in or multi-system ownership is
later approved.

## 33. Temporary Bridge Option

A mixed rollout may keep current `position_id` as `legacy_position_id` while new
episodes use canonical `episode_id`.

Allowed bridge use:

- ledger/report alias preservation;
- diagnostics and correlation;
- optional extra mismatch check during controlled migration.

Forbidden bridge use:

- copying it directly into canonical `episode_id`;
- deriving slot generation from it;
- generating a current episode from old queued work;
- treating equality as mutation authority;
- silently assigning native provenance to reconstructed state.

The bridge does not make legacy rows automatically fenced.

## 34. Implementation Split And Order

| ID | Scope | Readiness |
|---|---|---|
| D5A / D3D-1A | explicit slot key, durable episode authority field, slot generation, provenance, CAS | `BLOCKED_BY_SPECIFIC_PRODUCT_DECISION` |
| D3D-1B | immutable queue identity extension and legacy-item parser/drop | `BLOCKED_BY_D3D-1A` |
| D3D-1C | worker V1/V2 preflight and mutation claim before cancel/create | `BLOCKED_BY_D3D-1A_D3D-1B` |
| D3D-1D | conditional episode/generation/revision alias writeback | `BLOCKED_BY_D3D-1A` |
| D3D-1E | controlled legacy adoption/quarantine guard and provenance | `BLOCKED_BY_SPECIFIC_PRODUCT_DECISION` |

Specific decisions blocking D5A/D3D-1A and D3D-1E:

1. stable `account_principal_id` source/namespace for deployed processes;
2. controlled adoption versus quarantine-until-flat for active legacy positions;
3. automatic versus operator-approved activation for reconstructed exposure.

Dependency order:

```text
D5A/D3D-1A authority foundation
-> D3D-1B queue identity
-> D3D-1C preflight/mutation claim
-> D3D-1D conditional writeback
```

D3D-1E is rollout work parallel to B-D after the product/operations decision.
D3D-2 protection generation follows for complete replacement ordering.

## 35. D3D-1 Readiness

The D3D-1 design is complete, but behavior readiness is:

```text
D3D-1 = BLOCKED_BY_D5A
```

This is narrower than the prior generic D5/product block. D3D-1 does not wait
for scale-in, cross-system ownership, full POS-ID history migration, marker
authority, journal, or replay. It waits for the concrete current-slot episode
authority that the worker must compare.

D5A/D3D-1A itself remains blocked only by the three specific deployment/product
decisions listed above. Once D5A provides the field/authority contract in
production, D3D-1B/C/D are implementation-ready in dependency order.

## 36. Audit Result

| Exit criterion | Result |
|---|---|
| production diff | zero |
| exchange slot and episode definitions | explicit |
| canonical requirements/candidates | complete |
| current `position_id` ruling | `TEMPORARY_FENCE_ONLY` |
| canonical model | opaque UUID + slot generation + protection generation |
| creation/end authority | defined |
| ownership/generation/state/queue contracts | defined |
| V1/V2/V3 validation and CAS | defined |
| legacy position/queue/deployment/alias policy | defined |
| reconstructed episode/provenance | defined |
| scale-in/system relations | non-blocking and documented |
| marker/close/reconcile relations | scoped |
| fail-safe | no exchange mutation |
| true blockers | minimized and explicit |
| implementation split/order | defined |
| readiness | `BLOCKED_BY_D5A` |

**P10-06B PASS.**

P10-D3D-1 episode fence authority contract is **DESIGN AUDITED**. The worker's
future authority is now explicit, but queue and worker implementation must wait
for D5A current-slot episode authority. No production code, queue item, state,
Redis key, marker, migration, journal, or behavior changed.
