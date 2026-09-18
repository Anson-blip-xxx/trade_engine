# P10-07 / P10-D5A / P10-D3D-1A - Episode Authority Foundation Readiness

> Architecture and readiness only. Production code is unchanged. This document
> resolves the three remaining authority inputs before any episode field, slot
> generation, state schema, queue payload, worker, or migration is changed.

## 1. Scope And Blocker Recap

P10-06B reduced D3D-1 to three concrete authority inputs:

1. a stable `account_principal_id` for the exchange slot namespace;
2. a policy for already-active legacy positions without canonical authority;
3. a policy for exchange exposure reconstructed without trustworthy lineage.

P10-07 resolves them as follows:

```text
Account principal:
  explicit configured non-secret logical principal alias; missing = fail closed

Legacy active position:
  controlled adoption when strict preconditions and slot CAS succeed;
  otherwise QUARANTINED

Reconstructed exposure:
  automatically allocate authority only as RECONSTRUCTED_QUARANTINED;
  never auto-ACTIVE and never auto-create protection
```

These policies make D5A foundation implementation-ready in staged tickets. They
do not authorize implementation in P10-07.

## 2. Current Account Identity Inventory

| Candidate field/source | Exists now | Current meaning | Canonical principal suitability |
|---|---:|---|---|
| Binance API key | yes | authentication credential selected per prod/testnet | forbidden; secret-adjacent and rotation-scoped |
| Binance API secret/private secret | yes | request-signing secret | forbidden |
| Binance account UID/account ID | no | no endpoint/parser supplies one | unavailable |
| explicit subaccount ID | no | subaccount is implicit in credentials | unavailable |
| credential profile name | no | no configured profile abstraction | unavailable |
| configured logical account alias | no | proposed non-secret deployment identity | recommended future source |
| environment `demo`/`prod` | yes | endpoint/data environment from testnet flag | required slot dimension, not principal |
| sandbox flag/file | yes | local exchange-intercept runtime mode | required distinct environment, not principal |
| wallet balance/assets | yes | mutable account values | not identity |
| strategy `system` | yes | local ownership metadata | not exchange account identity |
| host/process ID | process runtime | deployment/process identity | unstable and not account identity |
| reconciliation `uid` | yes | random short lock-owner suffix | changes per call; not account UID |
| proxy address | no identity contract | transport routing if configured externally | not account identity |

Current code obtains one credential pair per process and sends the API key in
`X-MBX-APIKEY`. No production model persists which logical Binance account or
subaccount those credentials represent.

## 3. Secret Material Prohibition

The following must never appear in `account_principal_id`, slot keys, authority
records, queue payloads, logs, metrics, or exceptions:

- API secret or private key;
- full API key;
- reversible encoding of either secret;
- request signature;
- credential file contents.

A hash of an API key is less directly exposed but remains credential identity:
it changes on rotation and permits correlation of one credential. It may be an
optional redacted diagnostic fingerprint, never canonical account identity.

## 4. Principal Candidate Comparison

| Candidate | Stable/restart | Rotation-safe | Multi-account | Test/migration | Assessment |
|---|---:|---:|---:|---:|---|
| A. explicit config account alias | yes | yes | yes if uniqueness enforced | simple and deterministic | **selected** |
| B. Binance immutable account UID | potentially | yes | yes | API support absent in repo | future validation source only |
| C. hash(API key) | key-lifetime only | no | distinguishes credentials, not accounts | easy but misleading | rejected as canonical |
| D. credential profile name | profile-stable | potentially | yes | no profile abstraction today | possible alias source, not selected |
| E. deployment instance alias | host/deploy scoped | unrelated | collision/move risk | operationally fragile | rejected unless defined as account alias |
| F. exchange query identity | strongest if immutable | yes | yes | endpoint/contract absent | optional future verification |

## 5. Account Principal Contract

Selected conceptual configuration:

```text
BINANCE_ACCOUNT_PRINCIPAL_ID=<operator-assigned immutable non-secret alias>
```

Example values such as `binance-main` or `binance-subaccount-x` are illustrative,
not reserved formats.

Contract:

1. non-empty, normalized, version-compatible string;
2. unique per Binance account/subaccount within the authority store;
3. stable across restart, host movement, and API-key rotation;
4. bound operationally to one credential principal;
5. never generated randomly at process startup;
6. never inferred from API key, secret, balance, position, system, or host;
7. included in every canonical exchange-position key and authority record;
8. changing the configured alias is an explicit namespace migration, not a
   normal credential rotation.

An exchange-resolved immutable UID may later verify the configured alias, but
its absence does not justify deriving identity from credentials.

## 6. Missing Principal Policy

Options:

| Option | Risk | Decision |
|---|---|---|
| A. refuse authority enablement | explicit outage instead of identity collision | **selected** |
| B. derive temporary credential fingerprint | rotation splits one account; two keys split authority | rejected |
| C. silently use `default` | multiple accounts/environments collide invisibly | rejected |

Missing, empty, malformed, or conflicting principal means:

- do not create/adopt episode authority;
- do not allocate slot generation;
- do not enqueue authority-dependent protection;
- do not cancel/create/write authoritative protection state;
- expose a structured startup/health failure;
- classify observed exposure `RECOVERY_REQUIRED/QUARANTINED`;
- permit only read-only observation and the separately controlled emergency
  close policy.

## 7. Environment, Position Mode, And Slot Side

Canonical environment values:

| Runtime | Authority environment |
|---|---|
| production Binance endpoint | `PROD` |
| Binance demo/testnet endpoint | `DEMO` |
| local sandbox interception | `SANDBOX` |

Sandbox must be separate even when underlying config still says prod or demo.
Current `get_env()` returns only `demo`/`prod`; future principal/slot resolution
must include sandbox mode explicitly.

Current supported position mode is fixed:

```text
position_mode = ONE_WAY
slot_side = BOTH
```

Future implementation must validate/guard this assumption. It must not use
strategy LONG/SHORT as `slot_side`. Hedge support requires a separate design and
slot-preserving REST/WS/reconcile implementation.

## 8. Legacy Active Position Definition

A legacy active position is nonzero exchange/local exposure for which canonical
authority is absent or untrusted, including any missing:

```text
exchange_position_key
episode_id
slot_generation
identity_provenance
state_revision
```

Having current `position_id` does not remove legacy status. That value remains a
ledger/correlation alias, not mutation authority.

## 9. Legacy Policy Options

| Option | Safety | Availability | Decision |
|---|---|---|---|
| A. `QUARANTINE_UNTIL_FLAT` | strongest; no identity invention | no automated protection update | fallback when adoption cannot prove authority |
| B. `CONTROLLED_ADOPTION` | safe with exchange recheck, exclusive CAS, provenance | preserves managed operation | **preferred when all preconditions pass** |
| C. lazy assign on first ordinary read | concurrent readers can create split authority | convenient | rejected |
| D. deterministic migration from legacy ID | repeats old collision/split semantics | simple | rejected as canonical |
| E. legacy ID is canonical | no new authority or generation | compatible | explicitly rejected |

Final policy is conditional, not ambiguous:

```text
strict controlled-adoption preconditions pass -> MIGRATED ACTIVE
otherwise                                  -> QUARANTINED
```

## 10. Quarantine Semantics

Allowed while quarantined:

- exchange position/order/protection observation;
- health, metrics, and operator alerting;
- evidence and alias collection;
- controlled adoption attempt;
- explicitly authorized emergency full reduce-only close;
- post-effect reconciliation/accounting that preserves unknown provenance.

Forbidden while quarantined:

- new open or scale-in on the slot;
- new protection cancel/create/writeback;
- break-even, trailing, peak, or quantity protection replacement;
- partial close;
- normal strategy-driven lifecycle mutation;
- ghost/reconcile deletion as if ordinary managed state;
- automatic owner/system inference as authority;
- executing legacy queue items.

Existing exchange protection may be observed but is not adopted as current
desired protection merely because an `algoId` exists.

## 11. Controlled Adoption Preconditions

All conditions are mandatory:

1. complete canonical slot key and valid configured principal;
2. supported `ONE_WAY/BOTH` mode confirmed by deployment contract;
3. dedicated authority store and CAS are healthy and acknowledged;
4. mixed-version mutators stopped; old memory queues drained by restart/drop;
5. fresh nonzero exchange snapshot for the exact slot;
6. local symbol/side/quantity metadata is compatible within explicit tolerance;
7. no valid canonical current owner can be recovered;
8. no unresolved open/close/protection/adoption operation owns the slot;
9. exclusive mutation claim acquired for expected slot revision;
10. exchange evidence re-read after claim and before activation;
11. legacy ID retained only as alias;
12. one atomic CAS creates `MIGRATED` authority with next generation;
13. authority ACK observed before any new protection task;
14. conflicting or ambiguous evidence routes to quarantine.

Entry price, open time, quantity, and system metadata can aid consistency checks
but cannot prove episode identity.

## 12. Adoption Race And CAS Winner

Two adopters may independently generate candidate UUIDs. Exactly one can win:

```text
CAS expected = no current active owner + expected revision/high-water
winner       = installs candidate episode_id and next slot_generation
loser        = reloads canonical authority and discards its candidate
```

Loser outcomes:

| Reload result | Required action |
|---|---|
| same adoption already installed | return idempotent already-adopted result |
| another valid owner installed | accept winner; no overwrite or enqueue |
| conflicting owner/evidence | quarantine and alert |
| CAS acknowledgement unknown | `RECOVERY_REQUIRED`; query authority, no new UUID retry |
| exchange now flat | abandon candidate; do not activate |

A loser never continues with its private UUID and never emits protection work.

## 13. Legacy Protection And Close Policy

Before adoption reaches acknowledged `ACTIVE`:

```text
old/unfenced task -> DROP
new protection mutation -> FORBIDDEN
break-even/trailing replacement -> FORBIDDEN
```

Controlled adoption must precede any new protection mutation. Once adopted,
new work uses only the canonical episode/generation; old queue items remain
unfenced and are still dropped.

Emergency physical close remains available through a distinct break-glass
contract. Migration must not force an operator to keep unsafe exposure open.
The future emergency close:

- uses fresh exchange side/quantity and reduce-only semantics;
- serializes against adoption/normal mutation;
- records separate operation identity/reason/approver;
- handles timeout as UNKNOWN and re-queries;
- does not fabricate a native/migrated active episode merely to close;
- preserves unattributed provenance for later accounting.

Current close remains unchanged in P10-07 and currently requires a local row but
does not require canonical episode identity before exchange submission.

## 14. Recommended Legacy Policy

Final recommendation:

1. Deploy authority foundation with mixed-version writers stopped.
2. Drop all pre-deploy four-field queue work.
3. Attempt controlled adoption for each active legacy slot.
4. Adopt only when every precondition and CAS succeeds.
5. Mark successful authority `MIGRATED`; preserve legacy ID alias.
6. Quarantine every mismatch, unknown acknowledgement, or unsupported slot.
7. Permit observation and emergency full close while quarantined.
8. Prohibit all ordinary async protection mutation until ACTIVE.

This resolves the prior adoption-versus-quarantine blocker without requiring a
full historical migration.

## 15. Reconstructed Exposure Definition

A reconstructed exposure is exchange-confirmed nonzero exposure where no
trustworthy native or migrated current episode lineage can be recovered.

Current merge behavior synthesizes normal-looking system, time, stop, and
`position_id` fields. Those values provide operability, not authority, and must
not grant future ACTIVE mutation rights.

Minimum reconstructed states:

| Status | Meaning |
|---|---|
| `RECONSTRUCTED_QUARANTINED` | authority record exists with reconstructed provenance, but automated lifecycle mutation is denied |
| `RECONSTRUCTED_ACTIVE` | explicit policy/operator activation assigned ownership and approved desired protection behavior |

## 16. Reconstruction Activation Options

| Option | Identity result | Protection risk | Decision |
|---|---|---|---|
| A. automatic ACTIVE | immediate management | invents strategy/stop intent | rejected as default |
| B. automatic QUARANTINE | stable authority without invented intent | no automatic protection mutation | **selected foundation behavior** |
| C. manual authority creation | safest but delays identity allocation | old conflicts remain longer | not preferred |
| D. policy-based activation | can activate known safe classes | needs later product/risk policy | future transition from quarantine |

## 17. Reconstructed Authority Preconditions

Automatic `RECONSTRUCTED_QUARANTINED` creation requires:

1. valid account principal and unambiguous canonical slot;
2. supported position mode and side normalization;
3. fresh, stable nonzero exchange evidence;
4. no valid local owner or recoverable operation/alias authority;
5. exclusive CAS claim against expected revision/high-water;
6. new opaque episode ID and next durable slot generation;
7. immutable `RECONSTRUCTED` provenance;
8. no protection task, normal close, or ACTIVE transition as a side effect;
9. conflicting evidence produces `RECOVERY_REQUIRED`, not identity guessing.

Unknown old async work cannot prove membership in the reconstructed episode and
remains forbidden.

## 18. Recommended Reconstruction Policy

Final recommendation:

```text
exchange-only exposure
-> allocate one CAS-owned RECONSTRUCTED_QUARANTINED authority record
-> observe/alert/collect evidence
-> no automatic stop intent or strategy owner
-> explicit later policy/operator transition may make RECONSTRUCTED_ACTIVE
```

Whether and how to create protection for a reconstructed position belongs to D2
emergency/desired-protection policy. It no longer blocks D5A identity authority.

## 19. Provenance Model

Canonical immutable provenance values:

| Provenance | Meaning |
|---|---|
| `NATIVE` | episode activated from a managed confirmed flat-to-nonzero transition with durable lineage |
| `MIGRATED` | legacy active exposure passed controlled adoption and retained its legacy aliases |
| `RECONSTRUCTED` | exchange exposure existed but original lineage could not be proven |

Provenance never changes. Status may transition, for example
`RECONSTRUCTED_QUARANTINED -> RECONSTRUCTED_ACTIVE`, but provenance remains
`RECONSTRUCTED`. A new episode receives a new provenance decision.

## 20. Minimal Episode Authority Record

```text
schema_version
exchange_position_key
episode_id
slot_generation
status
provenance
legacy_position_id_alias?  # optional
revision
created_at
updated_at
```

This record owns current slot lineage and the generation high-water. It does not
contain order attempt history, operation stages, notification state, strategy
decisions, or an unbounded alias list.

Protection desired state and journal operations may reference this record but
do not replace its authority.

## 21. Authority Storage Options

| Option | CAS/restart | Failure semantics | Assessment |
|---|---:|---|---|
| A. embed only in `pm:positions` | no per-slot CAS; whole snapshot overwrite | swallowed writes/file fallback | rejected |
| B. dedicated Redis slot authority key/hash | Lua CAS possible; low latency | requires shared durable topology and explicit ACK | **selected minimum** |
| C. operation journal | strong relation to workflow | journal/backend not implemented; larger scope | future integration, not D5A minimum |
| D. PostgreSQL slot table | transactional conditional update | PG currently optional/write-only in runtime | viable alternate if made required |
| E. local JSON/in-memory | no distributed CAS/restart authority | host/process scoped | rejected |

## 22. Dedicated Slot Authority Model

Conceptual key:

```text
pm:slot:v1:<canonical-slot-key-digest>
```

The digest is over normalized non-secret slot dimensions. The full canonical
slot is stored in the record and compared, so a digest alone is not authority.

Rules:

- authority prefix is written only by a dedicated adapter;
- no generic `redis_store.set/delete` writes;
- no JSON file fallback or dual write;
- no exception swallowing;
- every mutation returns typed acknowledgement;
- record remains after flat to preserve high-water generation;
- Redis unavailability fails authority mutation closed;
- production use requires explicit shared Redis topology, persistence, backup,
  failover, and retention validation.

## 23. CAS Primitive Audit And Selection

Current Redis support includes:

- `SET key value NX EX ttl` for lock acquisition;
- Lua `GET == owner` then `PEXPIRE` for renewal;
- Lua `GET == owner` then `DEL` for release.

Current production does not provide:

- position/episode compare-and-set;
- state revision;
- `WATCH/MULTI/EXEC` transaction;
- `INCR` generation allocator;
- typed transition outcomes;
- acknowledged authority writes.

Selected future primitive is a dedicated Redis Lua compare-and-transition:

```text
compare_and_transition_slot(
  slot_key,
  expected_revision,
  expected_episode_id,
  expected_slot_generation,
  transition
) -> APPLIED | ALREADY_APPLIED | STALE | MISSING | INVALID | UNAVAILABLE
```

One Lua invocation loads, validates, allocates the next generation when needed,
writes the complete record, increments revision, and returns a typed result.
Lease helpers may coordinate work but cannot replace record CAS/generation.

## 24. Generation Persistence And Flat Transition

`slot_generation` is a durable high-water mark and never lives only inside the
active `pm:positions` row.

Exact convention selected:

- generation increments once per activated/adopted/reconstructed episode;
- first authority in a proven virgin/migration-initialized slot is generation 1;
- closing changes status to `FLAT` under expected revision/generation;
- flat transition does not reset or delete generation;
- authority record remains as the high-water tombstone;
- next flat-to-nonzero activation atomically allocates N+1;
- generation is never derived from UUID/time and never reused.

If an authority key is unexpectedly missing after namespace initialization,
treat it as authority loss, not generation zero.

## 25. Next Episode And Adoption Allocation

Native reopen:

```text
CAS current status=FLAT, generation=N, revision=R
-> episode_id=new opaque UUID
-> generation=N+1
-> status=ACTIVE
-> provenance=NATIVE
-> revision=R+1
```

Legacy adoption/reconstruction:

- if no authority namespace has ever existed for the slot during initial
  rollout, controlled bootstrap may create generation 1;
- if a retained authority record exists, allocate N+1;
- two adopters/reconstructors use the same CAS and only one wins;
- no hard-coded generation is accepted outside explicit bootstrap state;
- loser reloads the winner and drops its candidate UUID.

## 26. Product Decisions Versus Technical Decisions

### Policy decisions resolved by P10-07

| Topic | Selected policy |
|---|---|
| legacy active position | controlled adoption when strict preconditions pass; otherwise quarantine |
| reconstructed exposure | automatic authority only as `RECONSTRUCTED_QUARANTINED` |
| quarantined protection | no cancel/create/writeback or trailing/break-even updates |
| emergency close | allowed as explicit full reduce-only break-glass operation |
| normal open/scale-in on quarantine | prohibited |
| reconstructed protection/owner | requires later D2/product/operator activation policy |

### Technical invariants, not product questions

- explicit non-secret configured principal;
- secrets and credential hashes are not canonical account identity;
- separate PROD/DEMO/SANDBOX namespace;
- current mode fixed to ONE_WAY/BOTH;
- dedicated authority record with acknowledged CAS;
- immutable opaque episode ID and provenance;
- monotonic retained slot generation;
- exactly one CAS winner;
- legacy ID is alias only;
- missing/ambiguous authority fails closed;
- old four-tuples are dropped;
- whole-snapshot position state cannot be authority.

Later product decisions about reconstructed stop selection, protection SLO,
strategy ownership, attribution, and compensation remain in D2/D3 tickets. They
do not block D5A foundation.

## 27. Recommended Minimal Policy

```text
Account:
  explicit configured logical principal alias; missing fails closed

Environment/mode:
  PROD | DEMO | SANDBOX; current ONE_WAY/BOTH only

Legacy:
  controlled CAS adoption if exchange/local evidence agrees;
  otherwise QUARANTINED

Reconstructed:
  CAS-create RECONSTRUCTED_QUARANTINED authority;
  no automatic ACTIVE/protection

Slot authority:
  dedicated durable Redis record with Lua CAS and no file fallback

Generation:
  monotonic per slot, retained through FLAT, increment on new episode

Emergency close:
  always available through explicit break-glass full reduce-only contract

Async protection mutation:
  only acknowledged ACTIVE fenced episode work
```

## 28. D3D-1 Minimum Implementation Preconditions

1. principal resolver validates explicit alias and environment;
2. canonical slot-key value object/builder;
3. dedicated authority record and adapter;
4. Redis topology/durability validation and fail-closed behavior;
5. atomic Lua generation allocator/CAS with typed results;
6. retained FLAT/high-water transitions;
7. immutable provenance and legacy alias handling;
8. controlled adoption/quarantine service;
9. reconstructed quarantine creation service;
10. mixed-version deployment guard;
11. authority-independent emergency-close contract before rollout;
12. architecture and concurrency tests for one-winner behavior.

Once D5A foundation provides items 1-9, D3D-1B/C/D can carry and enforce that
authority. Journal/replay is not a prerequisite for the queue episode fence.

## 29. Implementation Split And Readiness

| Ticket | Scope | Readiness |
|---|---|---|
| P10-07B / D5A-1 | principal resolver, environment/mode normalization, canonical slot-key value object | `READY_FOR_IMPLEMENTATION` |
| P10-07C / D5A-2 | dedicated slot authority adapter, Lua CAS, generation high-water, typed ACK | `READY_AFTER_D5A-1` |
| P10-07D / D5A-3 | controlled legacy adoption and reconstructed-quarantine foundation | `READY_AFTER_D5A-2` |
| D3D-1B | queue identity extension and legacy item drop | `READY_AFTER_D5A_FOUNDATION` |
| D3D-1C | worker preflight/mutation claim | `READY_AFTER_D5A_FOUNDATION_AND_D3D-1B` |
| D3D-1D | conditional protection alias writeback | `READY_AFTER_D5A_FOUNDATION` |

D5A design is **DESIGN AUDITED** and its staged implementation is
`READY_FOR_IMPLEMENTATION`, beginning with P10-07B. Active-flow wiring remains
out of P10-07B; it is enabled only after D5A-2/D5A-3 and deployment checks.

D3D-1 readiness becomes:

```text
D3D-1 = READY_AFTER_D5A_FOUNDATION
```

## 30. First Production Ticket

First production ticket:

**P10-07B / D5A-1 Slot Namespace + Principal Resolver**

Minimum scope:

- add an immutable account/environment/mode/slot value object;
- read and validate explicit non-secret principal configuration;
- normalize PROD/DEMO/SANDBOX and ONE_WAY/BOTH;
- reject missing/malformed principal;
- provide deterministic canonical encoding/digest;
- unit-test rotation independence and account/environment separation;
- do not write Redis;
- do not alter position state, queue, worker, open, close, reconcile, or runtime
  admission behavior;
- do not wire it into active trading flow yet.

This is the smallest foundation with zero behavior change. D5A-2 then adds the
authority store behind an unused adapter before D5A-3 introduces adoption logic.

## 31. Implementation Guardrails

- Do not derive principal from any credential or secret.
- Do not silently default a missing principal.
- Do not let sandbox share PROD/DEMO authority namespace.
- Do not claim hedge support; current implementation is ONE_WAY/BOTH.
- Do not embed authority only in `pm:positions`.
- Do not use generic Redis/file fallback for authority.
- Do not delete slot authority at flat.
- Do not initialize generation to 1 except explicit virgin/bootstrap CAS.
- Do not lazy-adopt during ordinary load/merge.
- Do not make reconstructed authority ACTIVE automatically.
- Do not block emergency full close on successful migration.
- Do not allow protection mutation before acknowledged ACTIVE authority.

## 32. Audit Result

| Exit criterion | Result |
|---|---|
| production diff | zero |
| account inventory/principal contract | complete |
| secret identity | rejected |
| environment/mode/slot-side | explicit |
| legacy options/adoption race | resolved |
| legacy protection/emergency close | explicit |
| reconstructed states/policy | resolved |
| provenance/authority record | defined |
| storage/CAS/generation | selected and defined |
| flat/reopen/adoption allocation | defined |
| product/technical split | explicit |
| implementation prerequisites/split | explicit |
| first production ticket | P10-07B selected |
| D5A readiness | `READY_FOR_IMPLEMENTATION` |
| D3D-1 readiness | `READY_AFTER_D5A_FOUNDATION` |

**P10-07 PASS.**

P10-D5A readiness is **DESIGN AUDITED**. Its three authority inputs are resolved,
and staged foundation implementation may begin with P10-07B. No episode field,
generation, state/queue schema, worker, Redis key, migration, adoption, marker,
open, close, or reconcile behavior changed.
