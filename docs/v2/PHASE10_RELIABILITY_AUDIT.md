# Phase 10-00 - Reliability And Semantic Hardening Architecture Audit

> Base: Phase 9 closure `fa6133e`. This audit changes no production code and
> selects no implementation. It defines authority, identity, commit, durability,
> failure, and recovery boundaries before reliability behavior is changed.

## 1. Goal And Guardrails

Phase 10 asks what counts as success when concurrent workers run, a process
dies between external effects, or one persistence system succeeds while another
fails. P10-00 documents current facts and decision dependencies only.

The exact imported backlog is:

`T1-B`, `T5`, `PMB-23A`, `T12`, `PMB-26C1`, `PMB-26C2`, `PMB-24`,
`PMB-26B2`, `POS-ID`, and `PMB-4`.

No additional behavior ticket is imported. New findings below refine those ten
boundaries; no `P10-NEW-*` ticket is required by this audit.

## 2. Reliability Cluster Model

The proposed clusters are valid, with explicit cross-cluster edges:

| Cluster | Tickets | Shared reliability question | Cross-cluster dependency |
|---|---|---|---|
| A. Open Safety | T1-B, T5 | How is one open request committed once and protected promptly? | Needs request identity from D1 and crash policy from D3 |
| B. Close/Reconcile Durability | PMB-23A, T12, PMB-26C1, PMB-26C2 | Which partial effects are durable, retryable, or compensatable? | Needs identity from C and product delivery policy from D |
| C. Identity/Marker Model | POS-ID, PMB-4 | What identifies an episode, operation, exchange aggregate, and marker generation? | Foundation for A and B |
| D. Product Policy | PMB-24, PMB-26B2 | Is divergence repair a business event, and what delivery guarantee is promised? | Decisions can start independently; implementation couples back to B/C |

The cluster boundary is conceptual, not transactional. Exchange, Redis, PG,
ClickHouse, Telegram, files, and process memory currently commit independently.

## 3. Source-Of-Truth Matrix

| Concern | Primary truth | Secondary truth | Recovery source | Conflict winner now |
|---|---|---|---|---|
| Position existence | Binance WS/REST | Redis `pm:positions` | Binance snapshot | Exchange; local-only state enters ghost handling |
| Position quantity | Binance amount | Redis metadata quantity | Binance snapshot | Merge uses conservative `min(exchange, local)`, so runtime quantity is split-authority |
| Position side | Binance signed amount | Redis metadata | Binance snapshot | Exchange |
| Entry price | Binance weighted entry | Redis metadata | Binance snapshot | Exchange |
| Strategy/system ownership | Redis metadata | inferred S6/S8 from side | local file if present; otherwise defaults | Explicit local metadata, else inference |
| `position_id` | Explicit Redis metadata | local fallback generators | synthesize during merge/close | Existing explicit value wins; no exchange recovery |
| Exchange order/fill identity | Binance `orderId`/trade ID | PG event fields | exchange query/reconcile script | Exchange identity, but no durable episode alias map |
| Active AlgoSL | Binance active algo orders | Redis `algo_sl_id` | none automatically | Worker cancels exchange orders and applies latest queued local intent |
| Desired protection | In-memory task tuple/local `sl` | Redis position metadata | local polling only | No durable desired-state authority |
| Closed/recent marker | Redis `closed:{symbol}` timestamp | none | none | Marker while fresh, except visible exchange position self-heals it |
| External-position detection | Exchange position plus missing local metadata | Redis pending/seen | repeated exchange snapshot | Exchange triggers detection; Redis suppresses attempts |
| Event ledger | PG `trade_events` when write succeeds | raw logs | none | No runtime conflict resolution; PG is write-only |
| Episode/history ledger | PG `trade_episodes`; CH analytics row | Redis partial accumulator | limited CH analysis replay | No single cross-sink winner |
| Notification status | none | Redis seen means attempted | none | No delivery truth exists |
| Open request identity | none | symbol/side/system context | none | No authoritative idempotency key |

Operational model supported by code:

`exchange = physical exposure`, `Redis/file = operational metadata`,
`PG/CH = historical sinks`, and `process memory = disposable work/cache`.
Comments naming Redis or PG as a global source of truth do not match runtime
recovery behavior.

## 4. Commit Boundary Map

### 4.1 Active Production Open

S6/S8 call `strategies.shared_executor.open_position`, not the legacy PM
lifecycle open path:

```text
strategy/local and exchange prechecks
-> leverage/margin requests
-> Binance MARKET order                         [physical exposure commit]
-> parse/classify fill
-> generate position_id + update cache/state    [local publish attempt]
-> PG OPEN_ORDER_FILLED event                   [audit attempt]
-> Telegram/log                                 [notification attempt]
-> enqueue AlgoSL in process memory              [protection intent only]
-> return True

worker later:
queue pop -> exchangeInfo -> cancel old algo -> create AlgoSL
-> save algo_sl_id                               [protection acknowledgement attempt]
-> sleep 11 seconds                              [rate pacing after attempt]
```

Money is exposed when Binance accepts/fills the market order. Local code treats
the open as successful only at the final `True`, but Redis save, PG write, TG,
and AlgoSL placement do not share an acknowledgement. Protection is established
only when Binance accepts the AlgoSL, not when the task is enqueued.

The legacy `PositionLifecycleService.open_position` has a different sequence
and received T1-A's sequential duplicate fix. It has no in-repository production
caller. P10 designs must explicitly target the active shared-executor path and
decide whether the legacy path remains compatibility-only.

### 4.2 Full Close

```text
recent guard -> write closed marker
-> positionRisk #1 -> Binance reduce-only MARKET close [physical close attempt]
-> positionRisk #2 confirms flat                    [physical close commit]
-> cancel Algo orders
-> PG CLOSE_ORDER_FILLED event
-> record_trade(final=True): PG episode -> CH -> analysis -> TG
-> positions.pop -> Redis whole-snapshot save
-> return True
```

Exchange-flat and sandbox branches use different PG/record ordering. Save
errors are swallowed. Therefore exchange-flatness, ledger completion, local
removal, and `True` are distinct commit observations, not one commit point.

### 4.3 Partial Close

Close-order partial fill:

```text
marker -> exchange close -> remaining exchange qty
-> mutate local qty -> save
-> record_trade(final=False) Redis partial accumulator
-> PG CLOSE_ORDER_PARTIAL -> clear marker -> return False
```

Deliberate layered take-profit:

```text
exchange partial order -> mutate qty -> save -> log -> return None
```

The layered path has no marker, event, episode, CH, TG, or protection update.
The two partial meanings do not share a commit model.

### 4.4 Ghost Cleanup

```text
exchange snapshot proves symbol absent
-> acquire pm:ghost_close:{symbol}
-> pop local in-memory state
-> record_trade(final=True, ghost_cleanup=True)
-> mark closed -> release lock
-> monitor terminal whole-snapshot save
```

There is no exchange mutation. The physical fact was already "flat." The local
destructive mutation precedes an unacknowledged multi-sink recorder, and durable
state removal occurs later in the monitor save.

### 4.5 External Position Reconcile/Notification

```text
exchange snapshot -> marker self-heal if fresh
-> pending/grace/seen checks
-> write seen -> clear pending -> log -> TG
-> PG EXTERNAL_POSITION_DETECTED
-> assign merged symbol -> save complete snapshot
```

Seen is the current attempt commit. PG and merged-state persistence happen
afterward. A raising PG callable aborts current assignment, later symbols, and
snapshot save after prior marker/alert/TG effects have committed.

### 4.6 Commit Conclusion

All five paths are multi-step partial-commit flows. No Python return value proves
that exchange, Redis, PG, CH, TG, and protection agree.

## 5. Open Identity Inventory

| Identity | Exists before order? | Retry stable? | Restart stable? | Distributed stable? | Current use |
|---|---:|---:|---:|---:|---|
| Symbol | yes | yes | yes | yes | local/exchange duplicate scope and state key |
| Side | yes | yes for same intent | only if request retained | yes as value | order direction; not duplicate key |
| System | yes | yes for same intent | only if request retained | yes as value | local metadata; not sent to exchange |
| Signal/event ID | sometimes upstream | source-dependent | potentially | potentially | not passed into open order |
| Request/idempotency ID | **no** | no | no | no | absent |
| `position_id` | no, generated after fill | no if regenerated | yes only after successful persistence | value only | episode/accounting correlation |
| Binance `orderId` | no, returned after submit | stable once known | only if persisted/queried | yes | post-fill event and cancellation |
| Binance `clientOrderId` | **not supplied** | n/a | n/a | n/a | absent |

The existing `position_id` cannot deduplicate order submission. The PG event ID
deduplicates recording of one known exchange order, not creation of a second
order.

## 6. Concurrent Open Race

```text
A: local/exchange precheck empty
B: local/exchange precheck empty
A: MARKET order accepted
B: MARKET order accepted
A: writes symbol metadata and queues protection A
B: overwrites symbol metadata and queues protection B
```

Current code has no open-scoped lock, unique request row, broker client order
ID, durable reservation, or atomic exchange compare-and-open. The existing
monitor and ghost locks do not cover open. Same-side orders scale the net
position; opposite S6/S8 orders can offset or flip it in one-way mode. Separate
workers can also race `cancel-all -> create SL`.

T1-A corrected sequential ordering in the legacy lifecycle path. It does not
provide T1-B concurrency or retry idempotency on the active path.

## 7. T1-B Candidate Models

| Model | Concurrent requests | Sequential retry/ambiguous ack | Crash/restart | Distributed workers | Scale-in/cross-system | Main limitation |
|---|---|---|---|---|---|---|
| Per-symbol Redis lock | serializes cooperating workers | lock does not identify completed request | abandoned TTL/lease policy needed | same shared Redis only | symbol scope may prohibit intended scale-in | no broker ambiguity solution |
| Stable request key | identifies same logical request | supports retry state | durable only if persisted | yes if shared | needs duplicate-scope product contract | no exchange enforcement alone |
| Binance client order ID | broker-level duplicate/lookup | strongest for timeout retry | survives process death at exchange | naturally distributed | key derivation must distinguish scale-in | API semantics must be verified |
| DB unique open-attempt row | elects one owner and records stages | durable retry state | handles owner death with recovery policy | yes | explicit schema can model systems/sides | cannot transact with Binance |
| Hybrid reservation + client ID + reconcile | covers local race and exchange ambiguity | strongest combined model | explicit resume/reconcile | yes with leases/fencing | can represent intentional requests | highest operational complexity |

P10-00 does not select a model. Product must first define duplicate scope,
scale-in, opposite-side behavior, idempotent-success versus rejection, and
ambiguous exchange acknowledgement.

## 8. Protection Timing Model

The actual timing is not a fixed 11-second delay:

```text
MARKET accepted/fill
-> state publication
-> PG call (up to connection timeout)
-> TG call (up to transport timeout)
-> in-memory enqueue
-> idle poll delay 0..1s OR backlog delay
-> placement IO: exchangeInfo + list/cancel old + create AlgoSL
-> local algo_sl_id writeback
-> worker sleeps 11s before next task
```

Each predecessor contributes its IO duration plus roughly 11 seconds. A stalled
task blocks the one process-local FIFO worker. During the gap:

- local state is normally already published, though persistence can fail silently;
- monitoring can see and close the position on a later strategy loop;
- no exchange-resident stop is guaranteed;
- process death loses queued/in-flight work;
- restart starts an empty queue;
- no startup scan repairs `algo_sl_id == 0` or adopts unknown exchange stops.

## 9. T5 Candidate Designs

| Design | Latency | Failure/restart recovery | Exposure | Compatibility/complexity |
|---|---|---|---|---|
| Synchronous initial protection | removes queue wait | still needs ambiguous-response/retry policy | shortest normal gap | medium behavior change |
| Async, immediate priority first attempt | reduces backlog delay | memory-only variant still loses work | low while process lives | low/medium, incomplete alone |
| Durable protection queue | worker latency remains measurable | ack/retry/dead-letter survive restart | bounded by worker SLO | high infrastructure cost |
| Open/protection two-phase workflow | explicit filled/unprotected/protected stages | strongest resume semantics | policy can block success or compensate | very high; couples T12 |
| Emergency fallback | local monitor or immediate close if protection fails | depends on durable detection | limits prolonged exposure | product-sensitive compensation |

Required decisions include the fill-to-protection SLO, whether open success
requires confirmed protection, cancel-before-create policy, retry limits, stale
task suppression, scale-in quantity, and worker ownership.

## 10. Crash Matrix

| Point | Exchange reality | Local/marker state | PG/notification | Retry consequence | Current recovery |
|---|---|---|---|---|---|
| C1 order filled before local save | position open | state absent or stale; no marker | no open event/TG/SL task | same request can create another order | later exchange snapshot adopts defaults; original intent lost |
| C2 local save before SL | position open, no confirmed SL | position may exist with `algo_sl_id=0` | PG/TG may be partial prefixes | local/exchange checks may block reopen, but protection intent is lost | local polling only; no SL repair scan |
| C3 enqueue before worker executes | position open, no SL | in-memory task only | prior effects may exist | restart drops task | none automatic |
| C4 exchange SL succeeds before state update | position and SL exist | `algo_sl_id` missing/stale | unrelated | later worker may cancel unknown existing SL | exchange order remains, but no adoption scan |
| C5 exchange close succeeds before local save | exchange flat | marker exists; local position may remain | event/episode may be absent or partial | close/ghost retry can duplicate accounting | load/ghost/reconcile eventually converges state |
| C6 ghost state pop before ledger record | exchange flat | in-memory pop; later save may persist removal | no record/marker on raise; silent recorder loss possible | durable removal can eliminate retry source | no guaranteed recovery |
| C7 marker update before persistence/close completion | exchange may still be open | fresh symbol marker can hide/coordinate local state | none required | other process may skip; merge may self-heal marker | timestamp expiry or exchange-visible self-heal |
| C8 seen write before PG | exchange position exists | seen set, pending cleared | TG attempted; PG not durable | 24h suppresses PG/TG retry | expiry/fingerprint change only |
| C9 PG exception after TG | exchange position exists | marker/seen/pending side effects survive; merge save aborts | TG already attempted; PG raises | later symbols skipped; next attempt suppressed | later monitor cycle after dedup window |

## 11. PMB-23A Classification

PMB-23A is a product durability decision plus technical crash-consistency
design, not a safe mechanical reorder.

If `record_trade` is audit-only, pop-before-record can be accepted only with
explicit observability of lost records. If it is required durability, removal
must not complete before an acknowledged durable intent/record. Today the
recorder fans out to Redis partial state, PG episode, CH, analysis, and TG,
swallows many failures, and returns no structured acknowledgement.

Record-before-pop changes at-most-once/loss risk into replay/duplicate risk
because PG upsert, CH dedup, analysis, and TG have different idempotency. The
decision must name the durable sink and duplicate-versus-loss preference before
ordering changes.

## 12. T12 Decomposition

| Sub-ticket | Scope | Current gap | Dependencies |
|---|---|---|---|
| T12-A Open crash consistency | request -> order -> state -> protection | filled order can outlive request/state/SL intent | T1-B identity, T5 establishment model |
| T12-B Close crash consistency | marker -> exchange flat -> ledgers -> state removal | any prefix can survive and replay differently | PMB-23A, POS-ID, marker model |
| T12-C Save ordering/acknowledgement | whole-snapshot Redis writes and branch order | save invocation is not durable acknowledgement; LWW races | state authority and CAS/version design |
| T12-D Restart recovery | resume incomplete open/close/protection/notification | only opportunistic exchange/Redis merge exists | A-C plus durable operation stages |

"Save-order symmetry" is not a sufficient target. Moving one save cannot make
Binance, Redis, PG, CH, TG, and workers atomic.

## 13. PG Persistence Model

PG stores idempotent events and upserted episodes, but it is not read for
runtime recovery. Disabled/configuration/connection/SQL failures normally
return `False`; callers usually ignore it. Pre-`try` preparation errors or an
injected callable can raise.

Repository evidence supports "optional historical audit sink" as current
behavior, while comments also claim "transactional source of truth." It does
not support event sourcing or strong runtime durability. Product must choose
the intended contract before code normalizes `False` and exceptions.

### PMB-26C1

Decision: should PG `False`/raise fail reconciliation, or should PG be explicit
best-effort with mandatory logging/metrics? Solution families are fail-fast,
explicit best-effort, durable outbox, retryable PG-pending state, or circuit
breaker. C1 is decision-ready but not implementation-ready.

### PMB-26C2

Decision: after required persistence fails, which marker, seen/pending, TG,
merged state, snapshot, and later-symbol effects survive or retry? Solution
families are reordered PG-before-seen, staged statuses, per-symbol outbox,
isolation with retry, whole-batch staging, or compensation. C2 is blocked by
C1, PMB-26B2, T12, and save acknowledgement.

## 14. Position Identity Model

| Generator | Format | Creation | Stored/used | Stability problem |
|---|---|---|---|---|
| Strategy open | `system:symbol:entry:open_time` | after fill | Redis, PG event/episode, CH, partial key | too late for request idempotency; entry can change |
| Merge/adoption | `system:symbol:open_time` | exchange merge when ID absent | Redis then downstream ledger | reconstruction time can change identity |
| PM/ledger fallback | `system:symbol:entry:open_time` | close/settle when ID absent | episode/event/partial key | defaults can collide or diverge |
| Explicit metadata | arbitrary | caller-provided | preserved everywhere | unvalidated namespace |
| External detection | `external:symbol:side:entry:qty` | notification | PG event | mutable qty/entry rotates identity |
| Fill reconciliation | `binance:symbol:orderId` | offline script | PG event | order identity, not aggregate episode |

No exchange-native position ID is consumed in one-way mode. The exchange slot
is effectively account/environment/symbol with mutable side, quantity, and
weighted entry. A future model must distinguish `open_request_id`,
`exchange_order_id`, `fill_id`, `position_id` (episode),
`exchange_position_key`, and `close_operation_id`.

## 15. Identity Mismatch Matrix

| Path | Primary current identity | Secondary identity | Split-brain |
|---|---|---|---|
| Open precheck | symbol, side-blind | exchange nonzero position | no request identity |
| Open success | symbol state key | generated position ID + order ID event | concurrent orders overwrite one symbol record |
| Full/partial close | symbol lookup | explicit/fallback position ID and close order ID | episode ID can differ after reconstruction |
| Ghost cleanup | symbol set difference | metadata position ID | local destructive key differs from ledger key |
| Reconcile/adoption | symbol | side/system inference | reconstructed ID and defaults can change |
| Marker | symbol only | none | old/in-flight episode indistinguishable from new one |
| Episode ledger | position ID | symbol/system attributes | multiple formats split one economic episode |
| Event ledger | event/order/fill ID | free-text position ID | no enforced episode/order/fill alias relation |

POS-ID is `NEEDS_DESIGN`, not a formatting cleanup. Migration must preserve
historical aliases and live `trade:partial:{sha1(position_id)}` buckets.

## 16. Marker Model

Current marker:

```text
closed:{symbol} -> {"ts": wall_clock_float}
logical default window: 4h
physical Redis TTL: none
operations: independent GET / SET / DELETE
```

It is overloaded as close-attempt coordination, completed-close tombstone,
reopen cooldown, ghost dedup, and local-state filter. It is not a real lock or
state machine: there is no token, generation, owner, reason, operation ID,
position ID, CAS, or compare-and-delete.

Expired/malformed values fail open but remain stored. Malformed markers cannot
self-heal because the freshness check returns false. A fresh marker is cleared
whenever exchange still shows a position, which can also happen while a marked
close is legitimately in flight.

| Candidate | Solves | Does not solve | Dependency |
|---|---|---|---|
| A. current payload + lazy delete | stale-key cleanup with minimal change | ABA, identity, in-flight/tombstone ambiguity | race-safe compare/delete design |
| B. Redis TTL | bounded storage | variable windows, identity, blind clear | canonical logical/GC windows |
| C. position-ID marker | old/new episode separation | pre-order/open scope and missing IDs | POS-ID canonicalization |
| D. symbol+side marker | side contamination in hedge-like models | one-way episode generations and systems | product position-mode policy |
| E. lifecycle generation marker | lease/tombstone stages and replay | largest schema/migration surface | POS-ID + T12 state machine |

PMB-4 is blocked by the position/operation identity model. TTL alone is not a
reliability design.

## 17. Product Policy Boundaries

### PMB-24 Silent Reconcile

Direct `reconcile_all` is structurally internal destructive synchronization:
local-only metadata is popped and saved with no record, marker, lock,
protection cancellation, or business-close result. Product intent is still
unknown. The wired `state.load()` can also merge/adopt and notify exchange-only
positions before the explicit second comparison, so facade behavior is broader
than the isolated service contract.

Product must choose:

1. Internal repair: exchange absence prunes metadata without business close.
2. Business closure: confirmed flatness enters one close-finalization path.
3. Detection/quarantine: record discrepancy first, confirm it, then explicitly
   select business close or administrative prune.

No option is selected here.

### PMB-26B2 Notification Delivery

Current seen means "attempt authorized," not Telegram acknowledged. Product must
choose one explicit model:

| Model | Ack point | Retry | Main trade-off |
|---|---|---|---|
| A. at-most-once attempt | atomic claim before send | none | lowest duplicates, highest loss |
| B. at-least-once delivery | validated Telegram acknowledgement | until success | possible duplicates after ack-write crash |
| C. bounded retry | attempt state plus count/backoff/terminal status | finite | controlled loss and duplicates |
| D. escalation | bounded primary retry then alternate/operator channel | policy-defined | needs durable consumer and acknowledgement |

PG detection and TG delivery must be separate semantic states unless product
explicitly declares a PG-backed operator channel. Current PG events have no
delivery consumer or attempt table.

## 18. Failure-Domain Matrix

| Failure | Scope | Current blast radius | Current recovery |
|---|---|---|---|
| Redis down | state, locks, markers, dedup, partial accumulator | save may silently fail; monitor lock can skip; open gates rely more on exchange | retry connection; limited pre-existing file fallback |
| PG down | events/episodes | normal failure becomes ignored `False`; audit loss | none/retry absent |
| Binance REST down | prechecks, reconcile, monitoring, close confirmation | opens may fail open on some reads; destructive reconcile skips on bad response | later polling/WS |
| Algo worker crash | one process protection pipeline | pending/in-flight tasks lost; no retry | process restart creates empty worker |
| Process restart | all memory queues/caches/threads | protection, dedup, ghost/analyzer work lost | exchange + Redis reload only |
| TG down | operator notifications | open/close/external alerts may be lost | none; external seen suppresses 24h |
| One-symbol ghost exception | one symbol after PMB-23B | later symbols continue; failed symbol may already be popped | no guaranteed restoration |
| Local decode error | local metadata/cache | tends to empty/fail-open local state | file fallback then exchange reconstruction |
| Marker corruption | cooldown/dedup/filter | malformed marker acts absent and remains stored | overwrite/manual clear only |

## 19. Durability And Restart Matrix

| Data/work | Memory | Redis/file | PG/CH | Exchange | Restart survives? |
|---|---:|---:|---:|---:|---|
| Physical position | cache only | metadata copy | history only | yes | yes via exchange |
| Position metadata | `_POS_CACHE` | `pm:positions`; optional existing file | episode after close | no | Redis/file if available; otherwise defaults |
| AlgoSL task | yes | no | no | no until placed | **no** |
| Placed AlgoSL | local ID cache | `algo_sl_id` best effort | no | yes | exchange survives; mapping may not |
| Recent marker | no | Redis only | no | no | only if Redis persists |
| Ghost runtime queue | yes | no | no | no | **no** |
| Notification pending/seen | no | Redis only | detection event after attempt | no | Redis only |
| Trade event | no | no | PG if successful | order/fill exists separately | yes only if PG wrote |
| Partial accounting | no | Redis key by ID hash | final episode/CH later | fills exist | Redis only |
| Open idempotency key | **none** | **none** | **none** | client ID not supplied | **no** |

Restart recovery is opportunistic reconciliation, not transaction replay. There
is no durable operation-stage journal for opening, protecting, closing, or
notifying.

## 20. Distributed Assumptions

Repository evidence suggests one S6 process and one S8 process on one host,
sharing `pm:positions`, markers, and localhost Redis while owning independent
queues, caches, and workers. Existing distributed coordination covers monitor,
ghost close, and WS leader only.

Unsupported or unknown assumptions:

- actual systemd unit files, instance count, restart delay, and shutdown policy
  are absent from the repository;
- Redis is configured at `127.0.0.1`, so multi-host coordination is not proven;
- Redis persistence and deployment-time fallback files are unknown;
- multiple S6/S8 replicas are not proven safe;
- open has no distributed lock;
- whole-snapshot state writes are last-writer-wins;
- lock TTL renewal/fencing is not universal.

Phase 10 requires a deployment assumption before implementation: account,
environment, host, process count, and shared coordination service must be named.

## 21. Transaction Boundary Options

| Option | Helps | Cannot solve alone |
|---|---|---|
| No transaction + compensation/reconcile | T12 exchange/local divergence, ambiguous outcomes | cannot undo fills, delivered TG, or committed sinks |
| PG transaction | events + episodes + PG outbox | cannot include Binance, Redis, CH, or TG |
| Redis lock/CAS/transaction | T1-B serialization, marker/state atomicity | cannot resolve broker timeout or PG durability |
| Transactional outbox | durable PG/CH/TG/protection intent and retry | atomic only with its owning database |
| Durable event/work queue | T5, TG retry, asynchronous recording | requires idempotent consumers and authority model |
| Exchange-as-truth reconciliation | restart and ambiguous exposure recovery | cannot reconstruct original request/system/notification |

No single mechanism solves all tickets. Exchange effects require a saga or
reconciliation model even if local writes become transactional.

## 22. Dependency DAG

```text
P10-D1 Open Idempotency Model
  -> T1-B
  -> P10-D2 Protection Establishment Model

P10-D5 Position Identity + Marker Authority
  -> PMB-4
  -> P10-D1 request/episode distinction
  -> PMB-23A replay identity

P10-D4 Persistence Failure Policy
  -> PMB-26C1
  -> PMB-26C2

PMB-26B2 product delivery model
  -> PMB-26C2 notification stages

P10-D1 + P10-D2 + P10-D4 + P10-D5
  -> P10-D3 Crash Consistency Model
  -> T12-A/B/C/D
  -> PMB-23A implementation

PMB-24 product divergence model
  -> implementation depends on P10-D3 and P10-D5
```

## 23. Independent Versus Coupled Tickets

| Ticket | Classification | Required predecessor/design |
|---|---|---|
| T1-B | COUPLED | P10-D1; active-path and duplicate-scope decision |
| T5 | COUPLED | P10-D2; request/position/protection generation identity |
| PMB-23A | COUPLED | P10-D3/D4/D5; recorder authority and replay policy |
| T12 | COUPLED | P10-D1/D2/D3/D4/D5 |
| PMB-26C1 | INDEPENDENT decision | P10-D4 product durability decision |
| PMB-26C2 | COUPLED | C1, B2, T12/save acknowledgement |
| PMB-24 | INDEPENDENT product decision | implementation later couples D3/D5/PMB-23A |
| PMB-26B2 | INDEPENDENT product decision | implementation later couples C1/C2/outbox |
| POS-ID | INDEPENDENT design foundation | P10-D5 |
| PMB-4 | COUPLED | POS-ID/operation identity and P10-D5 |

## 24. Implementation Readiness

| Ticket | Readiness | Reason |
|---|---|---|
| T1-B | NEEDS_PRODUCT | duplicate/scale-in/cross-system scope and acknowledgement semantics undefined |
| T5 | NEEDS_PRODUCT | protection SLO and failure response undefined |
| PMB-23A | NEEDS_PRODUCT | duplicate-versus-loss and durable recorder sink undefined |
| T12 | NEEDS_DESIGN | must decompose and define saga/recovery across resources |
| PMB-26C1 | NEEDS_PRODUCT | PG durability role undefined |
| PMB-26C2 | BLOCKED_BY_OTHER_TICKET | requires C1, B2, T12, and save policy |
| PMB-24 | NEEDS_PRODUCT | internal repair versus business close unresolved |
| PMB-26B2 | NEEDS_PRODUCT | delivery guarantee/ack/retry/escalation unresolved |
| POS-ID | NEEDS_DESIGN | canonical episode/request/order aliases and migration undefined |
| PMB-4 | BLOCKED_BY_OTHER_TICKET | marker scope requires identity/operation model |

No imported ticket is `READY` for a production implementation.

## 25. Proposed Phase 10 Sequence

1. P10-01: approve P10-D1 active open-path, request identity, duplicate scope,
   and ambiguous exchange acknowledgement model.
2. In parallel, obtain the P10-D2 protection SLO/failure-policy decision and
   characterize end-to-end fill-to-protection latency under idle/backlog cases.
3. Define P10-D5 canonical episode/request/order/fill identities and marker
   lease/tombstone roles, including migration.
4. Decide P10-D4 PG, recorder, notification acknowledgement, and failure policy.
5. Build P10-D3 crash/restart saga model using D1/D2/D4/D5 outputs.
6. Only then scope behavior implementations for T1-B, T5, PMB-23A, T12, and
   PMB-26C2.
7. Product decisions for PMB-24 and PMB-26B2 can proceed early, but their
   implementations wait for the relevant durability/identity model.

## 26. Proposed Design Tickets

| ID | Artifact | Scope |
|---|---|---|
| P10-D1 | Open Idempotency Model | active path, request identity, duplicate scope, exchange ack/retry |
| P10-D2 | Protection Establishment Model | SLO, desired protection identity, queue/retry/reconcile ownership |
| P10-D3 | Crash Consistency Model | T12-A/B/C/D lifecycle stages, compensation, restart resume |
| P10-D4 | Persistence Failure Policy | PG/recorder/TG acknowledgements, best-effort vs required durability |
| P10-D5 | Position Identity And Marker Authority | canonical aliases, migration, close lease, tombstone/cooldown scope |

These are documentation/design tickets. They authorize no production change.

## 27. Decision Questions

Before implementation, product/architecture must answer:

1. What is one logical open request, and may intentional scale-in reuse a symbol?
2. Does open success require local state, event persistence, and confirmed SL?
3. What maximum fill-to-protection latency is acceptable?
4. Is missing accounting preferable to duplicate accounting after recovery?
5. Is PG required durability or optional audit?
6. What does external-notification seen mean: attempt, delivery, or escalation?
7. Is silent reconcile administrative repair, business close, or quarantine?
8. What defines one position episode in one-way aggregate mode?
9. Is a marker a lease, close intent, tombstone, cooldown, or separate records?
10. What deployment topology must locks and durable queues support?

## 28. Implementation Guardrails

- No production change before its design/product predecessor is approved.
- Do not use current `position_id` as a pre-order request key.
- Do not treat a Redis lock alone as retry idempotency.
- Do not describe enqueue as protection establishment.
- Do not treat save invocation, PG `False`, TG return, or Python `True` as a
  cross-resource commit acknowledgement.
- Do not solve PMB-4 with TTL alone.
- Do not merge detection, audit persistence, and notification delivery states.
- Preserve exchange-as-physical-truth recovery while defining metadata lineage.
- Require crash/concurrency/restart acceptance tests before each behavior change.

## 29. P10-00 Conclusion

Cluster boundaries, authority, commit points, crash states, identities,
durability, restart behavior, deployment assumptions, dependencies, and
decision questions are now explicit. Production behavior remains unchanged.

**P10-00 PASS. Recommended P10-01: P10-D1 Open Idempotency Model.**
