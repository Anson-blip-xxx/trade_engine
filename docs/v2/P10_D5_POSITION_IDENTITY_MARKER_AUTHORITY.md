# P10-03 / P10-D5 - Position Identity And Marker Authority

> Architecture and semantic design only. Production code is unchanged. This
> document separates identity domains before any ID normalization, migration,
> marker-key change, TTL, reopen policy, or reconciliation behavior is chosen.

## 1. Scope And Guardrails

P10-D5 answers two questions:

1. What lifecycle object does one position identity represent?
2. Why must work from an old episode be unable to mutate a later same-symbol
   episode?

It does not authorize:

- changing the current `position_id` value or generator;
- changing `pm:positions` keys or marker keys;
- adding Redis TTL or marker fields;
- changing reopen, cooldown, open-exclusivity, or reconcile behavior;
- changing protection queue identity;
- migrating any live or historical record;
- implementing POS-ID, PMB-4, T12, or protection generation.

## 2. Identity Taxonomy

The model requires separate domains. Equality in one domain never implies
equality in another.

| Domain | Identifies | Cardinality and relation | Current state |
|---|---|---|---|
| Request identity | one logical open command across retry/restart | many requests may contribute to one episode; a failed request creates none | absent on active path |
| Signal/event identity | one producer observation/delivery | one signal may create zero or multiple strategy requests | models support optional IDs; active producers do not provide one |
| Exchange order identity | one broker order | one request may have one or more order attempts; one episode has many orders | Binance `orderId` after ACK; no supplied client ID |
| Exchange fill identity | one broker trade/fill | many fills compose an order and episode | runtime open/close events leave `fill_id` empty; offline reconciliation records trade IDs |
| Exchange position key | one mutable broker exposure slot | many episodes occupy the slot sequentially | effectively account/environment/exchange/mode/symbol; code usually reduces it to symbol |
| Position episode identity | one economic lifecycle in an exchange slot | may contain multiple intentional requests/orders/fills | current `position_id` approximates this after fill but is not canonical |
| Strategy ownership identity | the strategy owner or ownership allocation | may be one owner, multiple allocations, or transferred ownership by policy | `system` metadata only; lost ownership is inferred from side |
| Protection intent identity | one desired protection specification for an episode | one episode has many replacements | absent; current queue tuple is symbol-scoped work |
| Protection generation | monotonic version of desired protection within an episode | retry keeps generation; changed stop/quantity advances it | absent |
| Marker/tombstone identity | one lifecycle operation/result or policy constraint | lease, tombstone, dedup, and cooldown need distinct roles | current `closed:{symbol}` timestamp merges all roles |

These IDs must not be collapsed into `symbol`, `position_id`, `orderId`,
`algoId`, marker timestamp, or wall-clock time.

## 3. Canonical Relation Model

```text
signal_event_id (optional source causality)
  -> open_request_id (one logical command)
  -> exchange_order_id / exchange_fill_id aliases
  -> position_episode_id (one aggregate exposure lifecycle)
       -> strategy ownership allocation(s)
       -> protection_intent_id + protection_generation 1..N
       -> close_operation_id(s)
       -> episode-scoped close lease/tombstone

exchange_position_key
  -> episode A -> flat -> episode B -> flat -> episode C
```

An alias graph is required because no single ID exists at every boundary.
Deriving every identity from one mutable string would recreate the same
ambiguity in a different format.

## 4. Current `position_id` Generators

| Path | Current format | Creation time | Included | Omitted |
|---|---|---|---|---|
| Active S6/S8 open | `f'{name}:{symbol}:{entry:.12g}:{now:.6f}'` | after exchange submission and fill classification | system, symbol, entry, local wall time | side, account, environment, request/order/fill aliases |
| Merge/adoption fallback | `system:symbol:open_time:.6f` | exchange/local merge when explicit ID is absent | inferred/preserved system, symbol, metadata or observation time | entry, side, exchange aliases |
| PM `_position_id` fallback | `system:symbol:entry:.12g:open_time:.6f` | close/settlement lookup when explicit ID is absent | available metadata fields | account, environment, order/fill aliases |
| Ledger fallback | same four-part formula | settlement when caller passes no ID | recorder arguments | lineage/aliases |
| Explicit metadata | arbitrary string | caller-defined | unknown | validation and namespace |
| External-position event | `external:symbol:side:entry:.12g:qty:.12g` | notification after exchange-only observation | mutable exposure fingerprint | episode start, system, account/environment |
| Offline fill reconcile | `binance:symbol:orderId` | historical fill import | symbol and one order | aggregate episode and strategy |

The active generator is in `strategies.shared_executor._update_pos_cache`:

```text
now = time.time()
position_id = f'{name}:{symbol}:{entry:.12g}:{now:.6f}'
```

It runs only after `execute_order(intent)`. Therefore it cannot identify or
deduplicate the pre-order request.

### Current stability properties

| Property | Active current ID |
|---|---|
| Timestamp-based | yes, local wall clock formatted to six decimals |
| Includes symbol | yes |
| Includes system | yes |
| Includes side | no |
| Includes account/environment | no |
| Retry-stable | no; a repeated successful attempt gets a new time |
| Restart-stable | only if explicit metadata persisted and survives |
| Reconstructable from exchange | no |
| Migration-safe | no; several incompatible formats and identity-critical consumers exist |

## 5. Collision And Split Analysis

Collision is not merely theoretical:

- two processes can generate the same formatted time if their clocks and
  rounded entry/system/symbol match; six-decimal formatting does not guarantee
  clock uniqueness;
- the legacy open stores integer-second `open_time`, so identical fallback
  fields within one second produce the same ID;
- missing fallback metadata can collapse to `:SYMBOL:0:0.000000`;
- explicit metadata accepts arbitrary duplicate IDs without validation;
- IDs omit account and environment while PG uses `position_id` as a global
  primary key;
- external fingerprints collide across separate identical reopen episodes and
  rotate inside one episode when weighted entry or quantity changes;
- reconstruction time can generate a different ID for the same surviving
  exposure after failed persistence/restart.

Concurrent same-symbol opens also overwrite the one symbol state slot even when
their generated IDs differ. Unique strings would not solve that lost ownership.

## 6. Current `position_id` Usage Inventory

| Surface | Actual use | Classification |
|---|---|---|
| `pm:positions[symbol].position_id` | preserve local episode correlation metadata | identity-critical lineage, but not the state key |
| PM close/ghost paths | pass explicit/fallback ID into ledger and PG events | identity-critical |
| Redis partial accounting | `trade:partial:{sha1(position_id)}` accumulates partial close slices | identity-critical |
| PG `trade_episodes` | `position_id` primary key | identity-critical |
| PG `trade_events` | free-text `position_id`; attribution view joins by exact equality | identity-critical |
| ClickHouse runtime row | JSON includes `position_id` | intended correlation; checked-in schema lacks the column |
| External-position event | synthetic fingerprint used as event and position ID | event dedup/correlation, but not linked to adopted episode |
| Reconcile/ghost | reconstruct or consume ID for accounting | identity-critical when recording occurs |
| Protection queue | does not carry `position_id` | no episode linkage |
| Telegram | open/close text omits `position_id` | observability-only |
| Logs | mostly symbol/system; protection logs symbol/`algoId` | observability-only |
| Analysis dispatch | does not carry `position_id` | observability/analytics only |

Changing the string format would affect persisted state, PG primary keys and
joins, live partial buckets, runtime ClickHouse payloads, and unknown external
consumers. No destructive replacement is safe without aliases and migration.

## 7. Ledger Linkage Audit

The strongest current episode chain is:

```text
Binance open orderId
-> PG OPEN_ORDER_FILLED(event_id=order:{orderId}:open, position_id=current ID)
-> explicit position_id survives in pm:positions
-> partial/full close event uses the same ID
-> Redis partial bucket and PG trade_episodes row use the same ID
```

It breaks when:

- the process fails after fill but before state/PG publication;
- state persistence fails and merge reconstructs a new ID;
- WS ghost metadata synthesizes system/open time from observation state;
- silent `reconcile_all` removes metadata without an event or episode record;
- external detection uses its own mutable fingerprint;
- offline fill reconciliation groups by order-based pseudo-position ID;
- PG writes fail or the explicit ID is lost.

Runtime open, partial-close, and full-close events leave `fill_id` empty. The PG
event table has no foreign key to episodes. The attribution view joins only by
exact `position_id`, so reconstructed aliases silently lose attribution.

## 8. External Compatibility Constraints

Known compatibility surfaces are:

- Redis `pm:positions` records containing legacy IDs;
- Redis partial keys derived from SHA1 of the exact ID;
- PG `trade_episodes.position_id` primary keys;
- PG `trade_events.position_id` and attribution joins;
- ClickHouse JSON rows that currently include the field;
- arbitrary metadata callers that may provide an ID;
- logs, exports, dashboards, or operator procedures outside this repository.

No repository code validates or parses a canonical colon format, but absence of
a parser does not make persisted references disposable. A future canonical ID
must preserve current values as aliases rather than rewrite them in place.

## 9. Symbol Identity Audit

`symbol` currently carries several distinct responsibilities:

| Use | Current shape | Hidden identity role |
|---|---|---|
| PM state | `pm:positions[symbol]` | one local active slot |
| Active cache | `_POS_CACHE[symbol]` | one process active slot |
| Duplicate open | `has_any_position(symbol)` / `symbol in positions` | position exclusivity policy |
| Exchange snapshot | parsed dict keyed by symbol | aggregate exchange-position key without account/env/mode |
| Closed marker | `closed:{symbol}` | lease, tombstone, cooldown, dedup, filter |
| Ghost lock | `pm:ghost_close:{symbol}` | close-accounting coordination |
| Reconcile | set difference by symbol | current exposure matching |
| Protection | queue/cancel-all/writeback by symbol | protection ownership |
| Recent/loss cooldown | symbol lookup | future admission policy |
| Notifications | external pending/seen keys by symbol plus mutable fingerprint | alert dedup |

Symbol is a useful lookup dimension and part of the exchange slot. It is not an
episode ID: the same slot hosts A, B, and later episodes over time. It is not a
request ID, owner ID, protection generation, or close-operation token.

## 10. Side And System Audit

### Side

Side is derived from signed exchange quantity and stored as metadata. It is not
part of the active `position_id`, state key, marker key, ghost lock, or
protection ownership key. Orders use one-way `positionSide='BOTH'` semantics.

Same-symbol opposite-side requests are currently rejected by side-blind gates.
If they race, orders can offset or flip aggregate exposure while one symbol row
survives. A direct sign flip must not silently retain the old episode identity.

### System

`system` records strategy ownership metadata and participates in active/fallback
ID strings. It is not sent to the exchange and is not part of state/marker keys.
When metadata is absent, merge infers `LONG -> S6` and `SHORT -> S8`.

Same-symbol different-system requests are compressed into one slot because:

- state and exchange snapshots are symbol-keyed;
- duplicate gates are symbol-wide;
- the exchange is treated as one-way aggregate exposure;
- local metadata has one `system` value, not ownership allocations.

Whether systems may co-own, transfer, or scale one episode is a product decision.
Current side inference is recovery metadata, not proof of original ownership.

## 11. Position Episode Definition

The candidate semantic definition is:

**A position episode is one continuous lifecycle of nonzero aggregate exposure
in one exchange position slot, beginning when confirmed exposure transitions
from flat to nonzero and ending when it is confirmed flat.**

Under this definition:

| Action | Episode result |
|---|---|
| First open from confirmed flat | creates a new episode |
| Intentional scale-in without crossing flat | same episode, new request/order/fills, if product allows scale-in |
| Partial close with nonzero remainder | same episode |
| Stop-loss or protection update | same episode, next protection generation |
| Full close confirmed flat | ends episode |
| Reopen after confirmed flat | always a new episode |
| Direct sign reversal without an observed flat sample | semantic old-close/new-open boundary; requires order/fill lineage to resolve |
| Accidental concurrent add/offset | not valid scale-in semantics; must be reconciled explicitly |

The exchange slot is mutable and reusable. Episode identity is immutable and is
never reused after flatness.

## 12. Reopen Semantics

Example:

```text
BTC episode A -> confirmed flat
five minutes later -> BTC exposure opens again as episode B
```

This is the same symbol and exchange slot but a new position episode. Any stale
callback, protection task, close result, or tombstone from A must be fenced from
B.

The current four-hour marker intentionally blocks repository-mediated reopen,
but it cannot identify A or B. Two semantics must be separated:

1. **Episode correctness:** A's lease/tombstone applies to A and must not mutate
   B.
2. **Cross-episode admission policy:** product may intentionally prohibit a new
   BTC episode for four hours after A closes.

If the cooldown remains, it should be an explicit policy record with declared
scope (account, symbol, side, system, reason) and expiry. It must not be inferred
from an episode tombstone.

## 13. Current Marker Model

```text
key:              closed:{symbol}
payload:          {"ts": wall_clock_float}
logical freshness: time.time() - ts < within_hours * 3600
default window:   4 hours
Redis TTL:        none
write:            unconditional SET
read:             GET plus timestamp comparison
clear:            unconditional DELETE by symbol
```

The payload has no side, system, episode ID, position ID, operation ID, owner,
reason, status, expiry time, schema version, generation, or CAS token.

Successful full close retains it. Failure, exception, no-fill, and partial close
paths clear it. Exchange-visible exposure clears a fresh marker during merge.
Expired or malformed markers evaluate false but remain stored.

## 14. Marker Role Inventory

| Role | Real current behavior? | Evidence/qualification |
|---|---:|---|
| Recent-close dedup | yes | close and ghost paths skip recent symbols |
| Reopen cooldown | yes | active and legacy opens reject within the logical window |
| Completed-close tombstone | yes | successful full close retains marker |
| Duplicate/stale-close suppression | yes | non-force close guard checks marker |
| Ghost accounting dedup | yes | ghost paths check it before recording |
| Local-state visibility filter | yes | marked local-only state is hidden on fallback load |
| Weak close lease | attempted side effect | marker-first ordering signals an in-flight close, but GET/SET is not atomic and has no owner/token |
| Lifecycle authority | no | exchange-visible exposure overrides it; it cannot prove close stage or episode |

One timestamp cannot safely implement all these roles. Lease, tombstone,
accounting dedup, and cooldown differ in owner, transition, retention, and
cross-episode scope.

## 15. Marker Authority Model

### Current conflict order

| Conflict | Current winner |
|---|---|
| Exchange-visible exposure vs fresh marker during merge | exchange; marker is deleted |
| Fresh marker vs strategy open | marker; open is rejected |
| Fresh marker vs local-only state | marker; state is filtered/hidden |
| Fresh marker vs non-force close/ghost accounting | marker; duplicate action is skipped |
| Force close vs marker | force close proceeds and overwrites timestamp |
| Expired/malformed marker vs any operation | operation proceeds; marker remains stored |

P9's intentional rule is formalized as:

**EXCHANGE PHYSICAL EXPOSURE > RECENT CLOSED MARKER.**

The future authority hierarchy should be:

1. Exchange observation is authoritative for physical exposure, side, and
   quantity.
2. Durable operation/episode state is authoritative for lineage, ownership, and
   lifecycle stage that exchange snapshots cannot provide.
3. Local active-position metadata is a materialized operational view and
   preserves explicit episode lineage when valid.
4. Typed marker records constrain only their declared operation/episode/policy
   scope; no marker can override observed physical exposure.

Exchange authority does not mean blind marker deletion is race-safe. A visible
position may be an old close still in flight or a new episode. Identity and
operation state decide which record may be cleared.

## 16. PMB-4 Reframe

PMB-4 is not merely "expired marker remains." It is:

**A symbol-scoped marker lacks episode identity, typed lifecycle authority, and
race-safe expiry/clear semantics.**

The facts support three subproblems:

| ID | Problem | Current consequence | Dependency |
|---|---|---|---|
| PMB-4A | stale marker retention | expired values remain indefinitely; larger windows/future timestamps can reactivate/prolong them | retention and GC policy plus race-safe cleanup |
| PMB-4B | cross-episode contamination | A's timestamp can block, hide, clear, or coordinate B because only symbol is known | canonical episode/operation identity and cooldown scope |
| PMB-4C | malformed marker handling | missing/invalid payload fails open, disables dedup/cooldown/filter, and remains stored | versioned schema, corruption policy, observability, safe cleanup |

PMB-4B is the correctness core. Fixing only PMB-4A can reduce garbage while
leaving ABA and cross-episode mutation intact.

## 17. TTL And Expiry Options

| Option | Retention | Identity correctness | Variable policy windows | Race safety | Main limitation |
|---|---:|---:|---:|---:|---|
| A. Current lazy timestamp check | unbounded physical retention | no | yes via caller window | no | stale/malformed garbage and blind clear remain |
| B. Redis TTL | bounded physical retention | no | one physical TTL unless rewritten | no | expiry can delete a still-needed lease/tombstone; no episode scope |
| C. Payload `expires_at` | logical expiry explicit | no by itself | yes | no by itself | still needs GC and compare/delete |
| D. Episode-scoped marker | separates A from B | yes for episode roles | cooldown must be separate | needs CAS/token | lookup/index and migration complexity |
| E. Marker generation/version | detects ABA/stale clear | yes with typed scope | yes | supports compare-and-clear | requires durable monotonic authority |

TTL is garbage collection, not identity. A correct model may use TTL as a
secondary retention bound after role, logical expiry, episode, and transition
semantics are defined.

## 18. Malformed Marker Semantics

Current outcomes:

| Stored value | `was_closed_recently` | Physical cleanup | Risk |
|---|---:|---:|---|
| missing key | false | n/a | normal absence |
| dict missing `ts` | false | none | dedup/cooldown/filter silently disabled |
| wrong top-level type | false through caught error/condition | none | same |
| wrong `ts` type | false through caught arithmetic error | none | same |
| stale timestamp | false | none | normally storage garbage; can be active under a larger requested window |
| future timestamp/clock rollback | true longer than intended | none | prolonged reopen/close block |
| Redis read failure | false | unknown | fail-open coordination |
| delete failure | prior value remains | none | prolonged block |

Malformed markers are both storage garbage and a correctness risk: they disable
duplicate-close, ghost-dedup, cooldown, and local filtering without an alert.
They also cannot self-heal because self-heal is entered only when the freshness
check returns true.

## 19. Marker Key Candidates

| Candidate | Reopen correctness | Cross-system isolation | Lookup simplicity | Migration/compatibility | Assessment |
|---|---|---|---|---|---|
| A. symbol | cannot distinguish sequential episodes | none | simplest/current | lowest change | insufficient for lifecycle ownership |
| B. symbol + side | separates hedge directions | none | simple | moderate | still conflates sequential episodes; side can flip in one-way mode |
| C. symbol + system | separates strategy policy | yes | moderate | moderate/high | system is not exchange ownership and co-ownership remains unresolved |
| D. position episode ID | separates reopen episodes | ownership stored explicitly | needs slot-to-current-episode index | high | correct key for episode tombstone/accounting lineage |
| E. lifecycle generation | fences ABA and stale callbacks | depends on ownership model | needs durable slot generation | high | strongest transition fence; should accompany episode identity |

Recommended architecture is not a single concatenated key. Use an exchange-slot
index to the current immutable episode plus episode/operation-scoped records and
generations. Keep a separate policy key only for intentionally cross-episode
cooldown.

## 20. Position Episode ID Candidates

| Candidate | Pre-order availability | Retry/restart | Partial fill/multiple orders | Scale-in | Limitation |
|---|---:|---:|---:|---:|---|
| A. UUID generated pre-order | yes | stable only if durably reserved/reused | can represent episode candidate before fills | later requests must attach, not generate a new episode | failed request may never activate an episode; must not equal request ID by accident |
| B. request-derived ID | yes | as stable as request | first request maps easily | multiple requests per episode make derivation wrong | conflates request and episode |
| C. exchange-order-derived ID | after ACK | exchange-stable once known | multiple orders/ambiguous ACK need aliases | each scale-in order differs | order is not aggregate episode |
| D. first-fill-derived ID | after first known fill | recoverable only if fill identity/query survives | later fills alias to it | can represent first exposure transition | unavailable before irreversible effect; fill ID scope/API must be characterized |
| E. opaque episode ID + alias graph | can be reserved before order or activated at first fill | durable by contract | maps all request/order/fill aliases | supports many requests in one episode | requires operation state, slot ownership, and migration |

E is the recommended semantic model. Whether the opaque ID is allocated at
request reservation or first confirmed exposure is a D1/D3 implementation
decision. Its value must not be derived from mutable entry, quantity, side,
system, or observation time.

## 21. Request And Signal Relation (D1)

```text
request_id -> may create or join -> position_episode_id
```

- A rejected/failed request may create no episode.
- The first successful request can create an episode.
- If scale-in is allowed, later distinct request IDs can join the same episode.
- Retrying the same request never creates a second episode merely because a lock
  expired or the response was ambiguous.
- Signal/event identity remains causal input, not the execution request key
  unless the producer contract explicitly makes deliveries stable and unique.

Therefore `request_id != position_episode_id` even when a simple first-open path
temporarily has a one-to-one relation.

## 22. Protection Relation (D2)

Protection must be scoped as:

```text
(exchange_position_key, position_episode_id, protection_generation)
```

`symbol` identifies only the reusable slot. `algoId` identifies one exchange
order attempt. Neither identifies desired protection for the current episode.

A retry of the same desired trigger/quantity keeps the generation. A changed
stop or approved quantity creates N+1. Before cancel, create, adoption, or local
writeback, workers must compare episode and generation. Close invalidates the
episode's pending generations before old work can commit.

## 23. Crash Consistency Relation (T12 / D3)

D3 recovery needs to answer:

```text
Which request/order/fill caused this exposure?
Which episode owns this local state?
Which protection and close operations are current?
Which old callback is stale?
```

Exchange snapshots answer only current physical exposure. D5 supplies the
identity graph and fencing dimensions required by T12-A/B/D. D3 then defines
durable stages, compensation, resume, and cross-resource recovery.

P10-D5 is therefore a prerequisite to D3, not a UUID-format cleanup.

## 24. Reconcile And Restart Mapping

Current `positionRisk` parsing retains symbol, signed side, quantity, weighted
entry, leverage, and margin. WS retains a similar aggregate and synthesizes
`system='?'` and `open_time=now`. Neither source contains the local episode ID.

Current merge behavior:

1. Preserve explicit local metadata when available.
2. Otherwise infer system from side.
3. Default open time to observation time.
4. Generate a synthetic ID and default stop/strategy fields.

This reconstructs operability, not lineage.

Future mapping order should be:

1. Preserve a valid current durable episode record for the exchange slot.
2. Resolve a durable open operation and characterized request/order/fill aliases.
3. Compare bounded recent exchange order/fill facts and exposure transitions.
4. If lineage remains unknowable, create an explicitly `RECONSTRUCTED` or
   `UNATTRIBUTED` episode with provenance and operator/reconcile policy.
5. Never claim that mutable symbol/entry/quantity/observation time proves the
   original episode.

`reconcile_all` currently cannot do this and must remain unchanged in P10-03.

## 25. Migration Semantics

Legacy live and historical data lacks a canonical episode ID and version.
The existing `migrate_existing_positions` helper is only a symbol-level copy
from legacy `state:s6` / `state:s8` position dictionaries into PM state. When a
symbol is absent it fills `system`, `side`, and `original_qty`; it does not add a
schema version, canonical episode ID, request/order aliases, marker generation,
or protection generation. No in-repository production caller was found. Redis
file-to-Redis migration similarly copies values without shape conversion.

Candidate strategies:

| Strategy | Benefit | Risk/limitation |
|---|---|---|
| A. Lazy assign on read | low rollout coordination | concurrent readers can assign different IDs; must preserve old alias and provenance |
| B. Reconstruct deterministic ID | repeatable from chosen fields | cannot prove historical episode; mutable/default fields split or collide |
| C. Versioned state migration | explicit old/new shape and resumability | needs schema version, migration state, rollback, and all-store plan |
| D. New positions only | avoids mutating active legacy rows | mixed semantics persist until every old episode closes; marker/ledger aliases still needed |

Recommended future direction is versioned, alias-preserving migration:

- retain legacy `position_id` as `legacy_position_id`/alias;
- assign canonical episode ID only under an explicit migration status;
- label reconstructed/adopted provenance;
- preserve live partial-bucket lookup until final settlement;
- avoid rewriting PG primary keys in place;
- treat state migration separately from operation recovery.

Product must decide whether active legacy positions are lazily adopted or only
new episodes receive canonical identity. No migration is implemented here.

## 26. Future Immutability Rules

| Field | Rule |
|---|---|
| `request_id` | immutable and reused for every retry of the same command |
| `position_episode_id` | immutable from activation through history; never reused after flat |
| `exchange_order_id` / `fill_id` | immutable external aliases with documented scope |
| `exchange_position_key` | stable slot identity including account/environment/mode dimensions |
| strategy ownership allocation | versioned transition; never inferred as identity from side alone |
| `protection_generation` | monotonic within one episode; retry does not increment |
| close operation ID | immutable per logical close command/recovery operation |
| marker generation/token | immutable per marker write/transition and required for compare-and-clear |
| schema version | describes shape only; not reused as lifecycle generation |

State revision, schema version, episode identity, and lifecycle generation are
different concepts and must not share one field.

## 27. Candidate Position State Schema

Minimal conceptual shape:

```text
schema_version
state_revision
exchange_position_key
symbol
side
system / ownership
position_episode_id
origin_request_id
exchange_order_ids
opened_at
closed_at (nullable)
protection_generation
legacy_position_id (nullable alias)
identity_provenance (native / migrated / reconstructed)
```

Must-have for the D5 minimum are immutable episode ID, exchange slot, origin
request link when known, identity provenance, and a concurrency/fencing value.
Order/fill aliases may live in a related operation/alias record rather than an
ever-growing position dictionary. This is a schema proposal only.

## 28. Candidate Marker Schemas

A single overloaded payload is not recommended. Minimum fields depend on role:

### Close lease / operation

```text
marker_type = close_lease
exchange_position_key
position_episode_id
close_operation_id
generation_or_token
created_at
expires_at
status
```

### Episode tombstone / accounting dedup

```text
marker_type = episode_tombstone
exchange_position_key
position_episode_id
generation
closed_at
reason (optional)
```

### Cross-episode cooldown, only if product requires it

```text
marker_type = reopen_cooldown
policy_scope (account/symbol/side/system)
source_episode_id
created_at
expires_at
reason
```

For every role, identity/token and logical timestamps are must-have. `symbol` is
useful for lookup/diagnostics but is not sufficient ownership. Reason is useful
policy metadata, not identity.

## 29. Reopen Race Matrix

| ID | Race | Current risk | Future identity result |
|---|---|---|---|
| R1 | old marker A + new episode B | symbol timestamp blocks or hides B; self-heal cannot classify it | A tombstone remains scoped to A; separate cooldown policy decides B admission |
| R2 | close A while open B races | marker/order effects can interleave in one symbol slot | slot generation serializes episode transition; request/exchange recovery decides winner |
| R3 | delayed clear from A after B opens | unconditional DELETE removes B/newer marker | compare-and-clear requires A's episode and expected token/generation |
| R4 | stale protection task A affects B | task cancels B protection, creates A stop, writes alias into B state | episode+protection generation checks fence cancel/create/writeback |
| R5 | reconcile resurrects state after close | stale whole snapshot or merge can recreate metadata with a new synthetic ID | durable episode stage/revision prevents old-state resurrection; exchange still decides exposure |
| R6 | restart sees marker but no local state | cannot tell in-flight close, completed A, cooldown, corruption, or B | typed marker plus operation journal and exchange observation determine recovery |

Additional ABA risk: close A writes a marker, close B overwrites it, then A's
failure unconditionally clears B's value. A generation/token is required even
when both operations refer to the same episode.

## 30. Generation Fencing

The reusable exchange slot needs a durable current-episode reference or
monotonic lifecycle generation:

```text
slot S generation 41 -> episode A
episode A closes
slot S generation 42 -> episode B
```

Every asynchronous mutation carries its expected episode and generation:

```text
if current_slot.episode_id != expected_episode_id: reject stale work
if current_slot.generation != expected_slot_generation: reject stale work
if protection.generation != expected_protection_generation: reject stale work
```

This fences:

- old async callbacks and delayed save results;
- old protection cancel/create/retry/writeback;
- old marker clear and self-heal actions;
- stale close finalization and accounting dedup;
- whole-state writers attempting to resurrect an older episode.

A TTL or lock without a fencing value cannot prevent a paused old owner from
committing after its lease expires.

## 31. Marker Clear Semantics

| API | Cross-episode safety | Same-episode ABA safety | Assessment |
|---|---:|---:|---|
| `clear(symbol)` | no | no | current behavior; unsafe blind delete |
| `clear(symbol, episode_id)` | yes if atomically compared | no when two operations/generations share episode | better but incomplete |
| `compare_and_clear(key, expected_generation_or_token)` | yes when key payload also binds episode | yes | recommended primitive |

Future clear should compare marker type, episode, and the exact operation token
or generation observed/written. A failed comparison means the marker belongs to
newer work and must remain. Self-heal should transition only a matching stale
tombstone/lease, not blindly delete whatever now occupies the symbol key.

## 32. Identity Source-Of-Truth Matrix

| Identity | Current source | Durable now? | Recoverable now? | Future authority |
|---|---|---:|---:|---|
| Signal/event | producer snapshot / optional model field | source-dependent; active ID absent | no active stable ID | producer contract when supplied |
| Open request | absent | no | no | durable D1 operation record |
| Exchange order | Binance response; PG field if write succeeds | exchange; local persistence partial | query contract uncharacterized | exchange plus durable alias |
| Exchange fill | Binance user trades/offline script | exchange; runtime often omits | bounded query only | exchange plus alias map |
| Exchange position slot | mostly symbol plus deployment context | implicit | physical exposure recoverable | explicit account/env/exchange/mode/symbol key |
| Position episode | explicit/fallback `position_id` metadata | Redis/PG if writes succeed | not from exchange alone | durable canonical episode record |
| Strategy ownership | local `system`; side inference fallback | Redis if saved | not reliably | versioned episode ownership record |
| Protection generation | absent | no | no | durable desired-protection state |
| Marker operation/tombstone | `closed:{symbol}` timestamp | Redis only | value survives if Redis does, ownership does not | typed episode/operation-scoped state |

## 33. Product Decision Questions

1. **Q1 Reopen:** Must every same-symbol reopen after confirmed flat create a
   new episode? This model recommends yes.
2. **Q2 Ownership:** May different systems co-own one aggregate episode, or must
   one owner exclusively hold/transfer it?
3. **Q3 Scale-in:** Is intentional scale-in part of the same episode, and how is
   it distinguished from duplicate/concurrent open?
4. **Q4 Restart:** Must episode identity and ownership remain stable across
   process, host, and coordinator failover?
5. **Q5 Marker scope:** Which marker roles constrain only the same episode, and
   is a separate cooldown intentionally allowed to constrain future episodes?
6. **Q6 Legacy migration:** Must active legacy positions receive canonical IDs,
   or do only new episodes use the model?
7. **Q7 Direct side flip:** Does a net sign change always finalize one episode
   and create another even if polling never observes exact zero?
8. **Q8 Reconstructed exposure:** Should unknown-lineage exchange exposure be
   adopted, quarantined, or operator-approved, and how is provenance labeled?
9. **Q9 Identity retention:** How long must request/order/fill/episode aliases
   remain queryable after close?
10. **Q10 Cooldown scope:** Is reopen cooldown account+symbol, side-specific,
    system-specific, reason-specific, or removed from marker authority?

Q1-Q6 block POS-ID implementation. Q5 and Q10 directly block PMB-4 behavior.

## 34. Recommended Layered Model

1. **Request identity:** D1 stable operation key and UNKNOWN-safe retry state.
2. **Position episode identity:** immutable identity for one continuous aggregate
   exposure lifecycle in an explicit exchange slot.
3. **Order/fill linkage:** durable aliases from requests and exchange effects to
   the episode; never derive episode from one order alone.
4. **Protection generation:** desired protection scoped to episode and monotonic
   generation as defined by D2.
5. **Marker/tombstone authority:** typed lease, episode tombstone/accounting
   dedup, and optional separate cross-episode cooldown.
6. **Migration/reconcile mapping:** versioned alias-preserving adoption with
   explicit reconstructed provenance when original lineage is unknowable.

No backend is selected. These semantic layers should remain separate even if a
future implementation stores several of them in one transactional database.

## 35. Minimal Phase 10 Candidate

```text
immutable position_episode_id
+ explicit exchange_position_key
+ origin_request_id when known
+ legacy ID preserved as alias
+ marker payload bound to episode and operation/generation
+ compare-and-clear
+ protection generation bound to episode
+ explicit reconstructed identity provenance
+ separate cross-episode cooldown policy
```

This minimum is sufficient to prevent old marker/protection work from silently
owning a new episode. It still needs product decisions, versioned migration, and
D3 operation stages before implementation.

## 36. Full Reliability Model

Must-have foundation:

- D1 durable request operation;
- immutable episode and explicit exchange slot;
- request/order/fill/episode alias graph;
- protection generation;
- typed marker roles and generation-aware transitions;
- versioned state and migration provenance.

Future hardening:

- full lifecycle operation journal and replay;
- per-episode state revision/CAS and distributed leases;
- automated bounded exchange order/fill reconstruction;
- historical backfill/alias repair;
- event-sourced projections and operator conflict tooling.

The full model belongs to P10-D3/T12 after D4 defines persistence authority.

## 37. Suggested Implementation Split

These are future planning tickets only:

| ID | Scope | Readiness |
|---|---|---|
| P10-D5A Episode Contract | exchange slot, episode boundaries, scale-in/side-flip/ownership/reopen semantics | BLOCKED_BY_PRODUCT_DECISION |
| P10-D5B Identity Alias Model | request/order/fill/legacy/protection linkage and retention | BLOCKED_BY_D5A_AND_API_CHARACTERIZATION |
| P10-D5C Versioned Migration | live legacy adoption, provenance, partial buckets, PG/CH compatibility | BLOCKED_BY_D5A_AND_PRODUCT_DECISION |
| P10-D5D Typed Marker Authority | lease/tombstone/cooldown schemas, generation, compare-and-clear | BLOCKED_BY_D5A_AND_PRODUCT_DECISION |

PMB-4 decomposition:

| ID | Scope | Readiness |
|---|---|---|
| PMB-4A | stale retention, logical expiry, GC/TTL policy | BLOCKED_BY_PRODUCT_DECISION |
| PMB-4B | cross-episode marker contamination and ABA fencing | BLOCKED_BY_POS-ID |
| PMB-4C | malformed/version handling, quarantine, metrics, safe cleanup | NEEDS_DESIGN; implementation waits D5D |

No ticket is authorized for production implementation now.

## 38. Implementation Guardrails

- Do not rename or replace current `position_id` without an alias/migration
  contract.
- Do not use `position_id` as a pre-order request key.
- Do not derive canonical episode identity from mutable entry, quantity, side,
  system, or observation time.
- Do not treat symbol, order ID, fill ID, or `algoId` as an episode ID.
- Do not silently define scale-in or cross-system ownership through key shape.
- Do not add TTL as the PMB-4 correctness fix.
- Do not let tombstone semantics silently impose cross-episode cooldown.
- Do not clear markers without comparing episode and operation generation/token.
- Do not infer original episode identity from exchange exposure alone.
- Do not migrate state without schema version, provenance, rollback, and legacy
  alias preservation.
- Do not change open, close, reconcile, protection, marker, or persistence
  behavior in P10-03.

## 39. Readiness And Conclusion

P10-D5 architecture is **DESIGN AUDITED**.

POS-ID implementation readiness is:

**BLOCKED_BY_PRODUCT_DECISION**

It requires approved episode boundaries, scale-in and direct-side-flip rules,
cross-system ownership, restart guarantee, identity retention, and legacy
migration policy.

PMB-4 implementation readiness is:

**BLOCKED_BY_PRODUCT_DECISION**

Its design now separates stale retention, cross-episode contamination, and
malformed handling, but behavior requires approved cooldown scope, marker-role
retention, and the canonical episode contract. PMB-4B also depends on POS-ID;
PMB-4C can proceed as a design characterization only.

Recommended P10-04 is **P10-D4 Persistence Failure Policy**. D4 can decide PG,
recorder, and notification durability now that D1, D2, and D5 identities are
explicit. P10-D3 remains next after D4 because its crash/restart saga depends on
the D4 acknowledgement authority as well as D1/D2/D5.

**P10-03 PASS.** Identity domains, current generators/consumers, episode and
reopen semantics, marker roles/authority, PMB-4 decomposition, migration,
reconcile mapping, race fencing, and future readiness are explicit. No
production code, ID, state key, marker, TTL, payload, reopen, reconcile,
protection, or migration behavior was changed.
