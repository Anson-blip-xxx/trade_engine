# P10-05 / P10-D3 - Crash Consistency Model

> Architecture and semantic design only. Production code is unchanged. This
> document defines durable operation progress, crash recovery, replay,
> reconciliation, and compensation using the audited P10-D1, D2, D4, and D5
> contracts. It does not select a journal backend or approve product policy.

## 1. Scope And Guardrails

P10-D3 asks:

```text
If a process disappears at any instruction boundary, what durable fact lets a
new process decide whether to submit, query, project, finalize, retry an
idempotent write, compensate, or stop for operator review?
```

This model covers:

- open submission and projection;
- full and partial close;
- protection create and replace;
- ghost finalization;
- external-position reconciliation;
- restart discovery, ownership, replay, UNKNOWN resolution, and compensation.

It does not authorize:

- adding an operation journal, outbox, transaction, lease, or CAS;
- changing the current bool/None APIs;
- changing exchange order parameters or retry behavior;
- selecting Redis, PostgreSQL, or a hybrid as journal authority;
- changing open, close, protection, ghost, or reconcile behavior;
- treating the candidate state machine as implemented behavior;
- implementing T12, PMB-23A, PMB-24, PMB-26C2, or D3A-D3E.

## 2. Current Runtime Facts

The current runtime does not recover lifecycle workflows.

```text
S6/S8 startup
-> load strategy cooldown state
-> shared_executor.reconcile_positions(name, state)
-> subscribe
-> enter strategy loop
```

`shared_executor.reconcile_positions` returns `state` unchanged. Later PM
monitoring can reconstruct the current exchange position snapshot, but it does
not discover an incomplete open, close, protection, ghost, or external-adoption
operation.

Current facts relevant to crash consistency:

- active open submits before any durable request or operation intent exists;
- `OrderIntent` has no request ID or exchange client order ID;
- Redis position state is an unacknowledged whole-snapshot overwrite;
- there is no position or operation compare-and-swap revision;
- PG trade events are not read by runtime recovery;
- the production journal default is `NullJournalRecorder`;
- the AlgoSL queue is process memory and starts empty after restart;
- ghost cleanup removes local metadata before close recording is proven;
- `reconcile_all` can silently remove ghost metadata;
- current locks and WS leases do not own a durable lifecycle operation;
- bool/None results collapse rejection, ambiguity, partial progress, and
  post-effect persistence failure.

The current implementation therefore offers opportunistic state
reconstruction, not crash-consistent workflow continuation.

## 3. Unified Operation Taxonomy

Every irreversible or recovery-relevant lifecycle workflow is represented by
one typed operation:

| Operation type | Semantic effect | Primary exchange effect | Required terminal fact |
|---|---|---|---|
| `OPEN` | create or increase physical exposure under an approved request | entry order | final open result plus projected episode/protection disposition |
| `CLOSE_FULL` | reduce the episode to flat | reduce-only close order or observed external flat | final accounting and episode tombstone/projection removal |
| `CLOSE_PARTIAL` | reduce but preserve the episode | partial close order | fill slice accounted and remaining projection updated |
| `PROTECTION_CREATE` | establish the first current-generation protection | create exchange algo order | matching current-generation protection verified |
| `PROTECTION_REPLACE` | supersede one protection generation with another | cancel/query/create according to replacement policy | new generation verified and old generation disposition known |
| `GHOST_FINALIZE` | finalize a locally tracked episode already observed flat | no new close order by default | close accounting plus local removal/tombstone |
| `EXTERNAL_RECONCILE` | classify and project/quarantine exchange exposure missing locally | normally query-only | adopted projection, quarantine, or approved business action |

Operation type constrains legal transitions. A generic retry loop must not turn
`GHOST_FINALIZE` into an exchange close or `EXTERNAL_RECONCILE` into an implicit
adoption policy.

## 4. Identity Model

The required identity relation is:

```text
operation_id != request_id != position_episode_id
```

Each operation also binds the exchange slot and the relevant generation:

```text
operation_id
  operation_type
  request_id?                 # OPEN command identity from D1
  exchange_position_key       # account/env/exchange/mode/symbol slot
  position_episode_id?        # immutable lifecycle episode from D5
  lifecycle_generation?       # episode/slot mutation fence
  protection_generation?      # desired protection version from D2
```

Rules:

1. `operation_id` identifies one recoverable workflow execution.
2. `request_id` identifies one logical open command across delivery and retry.
3. `position_episode_id` identifies one economic lifecycle, not one command.
4. `exchange_position_key` identifies the mutable broker exposure slot.
5. `lifecycle_generation` fences stale close, ghost, adoption, and projection
   work for an earlier slot occupant.
6. `protection_generation` fences stale protection cancel/create/writeback.
7. Exchange order, fill, and algo IDs are aliases/evidence, not replacements
   for these identities.

An `OPEN` operation initially has no episode ID only if the selected episode
contract requires allocation after exchange evidence. The operation must still
retain its request ID and slot key. Once bound, episode identity is immutable.

## 5. Durable Operation Record

The candidate minimum durable record is:

| Field | Purpose |
|---|---|
| `operation_id` | immutable journal key |
| `operation_type` | transition and recovery policy |
| `request_id` | open idempotency relation when applicable |
| `exchange_position_key` | exchange query and slot serialization scope |
| `position_episode_id` | episode attribution and ABA fence |
| `lifecycle_generation` | stale lifecycle-work rejection |
| `protection_generation` | stale protection-work rejection |
| `stage` | last acknowledged operation stage |
| `version` | optimistic CAS revision |
| `owner_token` | current lease/fencing owner |
| `lease_expires_at` | bounded recovery takeover time |
| `input` | immutable normalized command/specification |
| `exchange_aliases` | client/order/fill/algo IDs and query keys |
| `effect_summary` | observed quantity, side, price, status, and timestamps |
| `pending_requirements` | required projection/accounting/finalization work |
| `last_error` | categorized error without redefining truth |
| `next_attempt_at` | retry scheduling for safe local/idempotent work |
| `created_at` / `updated_at` | audit and stuck-operation detection |

Immutable fields must reject mutation. Mutable fields advance only through an
atomic compare-and-swap on `(operation_id, version, owner_token)`.

Large payloads may live in an append-only attempt/evidence table, but the
recovery reader needs one authoritative current stage and version. Logs alone
cannot satisfy that requirement.

## 6. Candidate State Machine

| State | Durable meaning | Recovery action |
|---|---|---|
| `NEW` | operation object allocated but intent durability not yet acknowledged | persist/validate intent or abandon before any exchange effect |
| `INTENT_DURABLE` | immutable intent and identity are acknowledged | submission may be claimed if policy permits |
| `SUBMITTING` | fenced owner is entering an exchange mutation window | expired owner must query before any repeat |
| `UNKNOWN` | exchange mutation may have happened; outcome is not proven | query by aliases/slot/evidence; never blind resubmit |
| `EXCHANGE_ACKED` | exchange acknowledged an order identity, not necessarily final effect | query final order/fills/position as required |
| `EFFECT_CONFIRMED` | physical fill, flat state, or active protection is proven | perform required local/accounting projection idempotently |
| `LOCAL_PROJECTED` | required operational state reflects the confirmed effect | perform remaining required finalization |
| `AUXILIARY_PENDING` | core result is committed; retryable auxiliary fan-out remains | retry only explicitly durable auxiliary work |
| `COMPLETED` | all required core stages and selected completion obligations are acknowledged | no business-effect replay |
| `FAILED_RETRYABLE` | no ambiguous exchange effect exists and the named action is safe to retry | claim and retry the named stage |
| `FAILED_TERMINAL` | no automatic continuation is permitted | operator/product resolution |
| `COMPENSATION_REQUIRED` | forward completion failed after physical effect and policy requires a new mitigating operation | create/track a separate compensation operation |

`UNKNOWN` is not an error label. It is a correctness state. It preserves the
possibility that an irreversible effect occurred.

### Legal transition shape

```text
NEW -> INTENT_DURABLE -> SUBMITTING
                         |      |
                         |      +-> EXCHANGE_ACKED -> EFFECT_CONFIRMED
                         |                              |
                         +-> UNKNOWN -------------------+
                                                        v
                                              LOCAL_PROJECTED
                                                        |
                                      +-----------------+----------------+
                                      v                                  v
                              AUXILIARY_PENDING                      COMPLETED

Known-safe failures -> FAILED_RETRYABLE -> prior safe stage
Policy refusal      -> FAILED_TERMINAL
Post-effect hazard  -> COMPENSATION_REQUIRED -> separate operation
```

Terminal failure before an exchange effect must include evidence that no effect
occurred. Otherwise the state remains `UNKNOWN`.

## 7. Commit Points

There is no single distributed transaction across the exchange, journal,
Redis/file, PG, ClickHouse, and Telegram. The model therefore names several
commit points:

| Commit point | Meaning |
|---|---|
| Intent commit | durable operation identity/input exists before exchange mutation |
| Submission commit | exchange has an acknowledged order alias; not equivalent to fill |
| Effect commit | exchange evidence proves the physical effect or protection state |
| Projection commit | acknowledged local operational state represents that effect under the current generation |
| Accounting commit | authoritative close/fill record acknowledges the event once by identity |
| Finalization commit | required tombstone/removal/completion state is acknowledged |
| Auxiliary commit | optional notification/analytics delivery is acknowledged under its own policy |

The business operation is not globally atomic. Crash consistency means each
commit is identifiable and every later step is replayable or reconcilable.

## 8. Operation Ownership, Leases, And CAS

A worker may mutate an operation only while holding a durable lease containing:

```text
operation_id + owner_token + lease_expires_at + operation_version
```

Required rules:

- acquire and renew by atomic compare-and-swap;
- use a unique monotonically fenced token or equivalent generation;
- validate ownership immediately before exchange mutation and required write;
- persist `SUBMITTING` before entering an exchange mutation window;
- after lease expiry, a new owner treats `SUBMITTING` as potentially ambiguous;
- a stale owner cannot advance the journal, project state, clear a marker, or
  overwrite current protection aliases;
- slot-level coordination serializes incompatible operations, but operation
  identity remains the retry truth.

`LOCK != RETRY IDEMPOTENCY`. A lock expiring after exchange acceptance does not
prove that another submission is safe.

## 9. Generation Fencing

Every replay validates both identity and generation before mutation:

| Work | Required fence |
|---|---|
| open projection | request/operation binding plus current slot generation |
| full/partial close | episode ID plus lifecycle generation |
| ghost finalization | episode ID plus lifecycle generation plus observed-flat evidence |
| protection create/replace | episode ID plus protection generation |
| external adoption | exchange slot plus newly allocated or approved episode generation |
| tombstone/marker clear | exact episode/operation generation, never symbol alone |

If the current slot contains episode B, replay for episode A must stop even when
the symbol and side match. This is the D5 ABA rule applied to every D3 stage.

## 10. Exchange Reconciliation Evidence

The default evidence priority is question-specific:

| Question | Evidence priority |
|---|---|
| Did a submitted order exist and what happened? | direct order query by stable client/order alias, then fills/trades by alias, then bounded position-delta evidence |
| What is physical exposure now? | exchange position snapshot |
| Is current protection active? | direct algo-order query plus episode/generation/spec match |
| Which operation stage was acknowledged? | authoritative operation journal |
| What local metadata is currently projected? | revisioned Redis/file operational state |
| Was accounting finalized? | selected authoritative accounting sink, then immutable event/fill evidence |

The journal cannot overrule exchange physical truth. The exchange snapshot
cannot reconstruct request identity, episode lineage, accounting completion, or
the cause of a netted position change by itself.

Absence from a position snapshot is not enough to prove that an entry order was
never filled: the exposure may have filled and later closed or netted. UNKNOWN
resolution must use the strongest available aliases and bounded historical
evidence.

## 11. UNKNOWN Resolution

On an ambiguous exchange call:

1. durably set `UNKNOWN` if possible; if that write is itself uncertain, the
   pre-call `SUBMITTING` state is interpreted as UNKNOWN after lease expiry;
2. retain the same operation and request identities;
3. prohibit blind resubmission;
4. query direct exchange order status by stable client/order identity;
5. query fills/trades and current position/protection state as needed;
6. bind discovered aliases and evidence through CAS;
7. transition to `EXCHANGE_ACKED`, `EFFECT_CONFIRMED`, a proven safe retry
   state, or `FAILED_TERMINAL`/operator escalation;
8. remain UNKNOWN while the query plane is unavailable or evidence conflicts.

Candidate UNKNOWN timeout is an escalation deadline, not permission to assume
failure. Product must choose the timeout, operator action, and whether a later
manual command creates a distinct operation.

## 12. Replay Classification

Recovery classifies actions, not whole operations, into three groups:

| Class | Examples | Rule |
|---|---|---|
| Idempotent local replay | journal CAS, keyed projection upsert, keyed accounting insert, generation-matched tombstone | repeat with the same identity until acknowledged |
| Reconciled exchange replay | open/close submit, protection cancel/create | query first; submit again only after positive proof that no prior effect can exist |
| Non-replayable auxiliary | unkeyed Telegram send, legacy logs, unkeyed analytics dispatch | do not automatically repeat unless a durable delivery identity/outbox is introduced |

At-least-once recovery is safe only when the target write deduplicates by the
operation/event identity. Retryability is a property of a specific stage and
sink, not of an exception type alone.

## 13. Restart Recovery Algorithm

Before admitting new strategy opens, a recovery coordinator must:

1. verify journal availability and authority health;
2. scan nonterminal operations by stage, age, and lease expiry;
3. group operations by `exchange_position_key`;
4. read current exchange position and required order/algo/fill evidence;
5. acquire one operation using CAS and a fresh fenced owner token;
6. reject stale episode or generation work;
7. interpret expired `SUBMITTING` as UNKNOWN;
8. resolve UNKNOWN before any exchange resubmission;
9. replay idempotent required projection/accounting/finalization stages;
10. schedule durable protection work for current episodes/generations;
11. quarantine conflicting evidence instead of guessing;
12. mark core-complete operations `COMPLETED` or `AUXILIARY_PENDING`;
13. expose unresolved counts/age and apply degraded admission policy;
14. only then release eligible slots for new operations.

Recovery is continuous, not startup-only. A crashed worker can leave work that
another live process claims after lease expiry.

## 14. Open Timeline And Crash Matrix

Candidate durable open sequence:

```text
O1 allocate operation/request identities
O2 acknowledge immutable intent
O3 claim lease and persist SUBMITTING
O4 submit with stable exchange client identity
O5 reconcile ACK/fill and persist exchange evidence
O6 create/bind episode and persist local projection
O7 persist required open accounting/event
O8 create durable protection desired state, then complete according to product
```

| Cut | Crash point | Durable/effect possibility | Required restart action |
|---|---|---|---|
| O1 | before intent durable | no valid exchange submit is permitted | discard `NEW` or retry intent persistence under the same caller request identity |
| O2 | after intent durable, before submission | intent exists; no submit window entered | claim and submit once if slot/request policy still permits |
| O3 | after `SUBMITTING`, before exchange call | no effect or call may have started near crash boundary | query stable client ID first; only proven absence permits same-ID submit |
| O4 | after exchange ACK or timeout, fill unknown | order may be live, filled, rejected, or absent | enter/retain UNKNOWN; query order, fills, and position; never create a new request ID |
| O5 | after fill, before local projection | physical exposure exists without managed metadata | persist effect evidence, create/bind episode, project state; do not resubmit open |
| O6 | after local projection, before required ledger | exposure and local state exist; audit/accounting incomplete | idempotently write required event by operation/order/fill identity |
| O7 | after ledger, before durable protection handoff | open is accounted but protection work may be absent | create current-generation desired protection; apply unprotected-exposure policy |
| O8 | after protection handoff/verification, before return | durable work/result exists; caller may have seen no success | replay completion only; same request lookup returns structured prior/pending result |

The intent commit must precede O3/O4. If the journal is unavailable before
submission, a new open is blocked under the candidate safe policy.

## 15. Open Commit And Compensation

The irreversible open commit is confirmed exposure, not local cache insertion
and not function return.

Candidate core completion has two product variants:

| Variant | Open core result |
|---|---|
| Exposure-accepted | `LOCAL_PROJECTED` plus durable current-generation protection work is enough; result is pending protection |
| Protection-required | matching protection must be verified before `SUCCESS` |

If projection or protection cannot be established after fill, the operation
must not return ordinary failure. It remains recovery-required or creates a
separate compensation close if product policy requires risk reduction.

A compensation close:

- has its own `operation_id` and type `CLOSE_FULL`;
- links to the failed open operation as cause;
- uses the same episode and current generation;
- may itself become UNKNOWN;
- does not erase the open fill or rewrite history as if open never occurred.

## 16. Full Close Timeline And Crash Matrix

Candidate full-close sequence:

```text
C1 persist close intent for exact episode/generation
C2 acquire fenced operation/slot lease
C3 persist SUBMITTING and submit reduce-only close
C4 prove partial remainder or flat exchange effect
C5 persist authoritative fill/final accounting
C6 project remaining quantity or remove exact episode; persist tombstone
C7 complete operation and return structured result
```

| Cut | Crash point | Durable/effect possibility | Required restart action |
|---|---|---|---|
| C1 | before close intent durable | no operation-owned submit permitted | leave episode unchanged; caller may retry with stable command identity |
| C2 | after intent/lease, before exchange submit | no effect expected, but expired `SUBMITTING` is queried | verify episode generation and order absence before submit |
| C3 | after submit, before ACK/fill known | close may be absent, partial, full, or rejected | UNKNOWN query by order alias/fills and current exposure; no blind second close |
| C4 | exchange flat/partial before ledger | physical effect is authoritative; accounting incomplete | persist effect evidence and idempotent fill/final accounting |
| C5 | ledger acknowledged before local projection/finalization | accounting exists; local quantity may be stale | generation-fenced projection update/removal; do not record duplicate close |
| C6 | local save/removal after finalization work but before final marker/completion | exchange/accounting/local state may be complete | compare exact episode/tombstone and idempotently complete missing final marker/stage |
| C7 | completion marker durable before return | caller may observe timeout despite complete close | return prior structured result for same operation; no business replay |

Current code does not implement this order: it uses a symbol timestamp marker,
submits without an operation journal, and several required writes have no ACK.

## 17. Partial Close Crash Matrix

A partial close is not a failed full close. Each accepted fill slice requires a
stable fill/order alias and an episode-scoped accounting identity.

| Crash point | Risk | Required recovery |
|---|---|---|
| before intent durable | accidental untracked reduction if submit were allowed | prohibit submit |
| intent durable before submit | duplicate command delivery | one operation claim; verify episode/generation |
| submit ambiguous | duplicate reduction can over-close/flip when retry is not safely reduce-only | UNKNOWN; query order/fills/exposure before any retry |
| fill before accounting | reduced exchange quantity but missing realized slice | record exact discovered fill(s) idempotently |
| accounting before projection | local quantity stale | derive current physical remainder and generation-fenced save |
| projection before operation completion | caller sees no result and retries | same operation returns prior/pending result; no new submit |
| full flatten during intended partial | episode is physically flat | transition to full-finalization requirements; never preserve a phantom remainder |

The current `partial_close` mutates quantity and saves after exchange response,
returns `None`, has no fill ID, and has no durable replay identity. Its exact
consistency guarantee is therefore not defined today.

## 18. Protection Create And Replace Crash Matrix

Protection uses D2 episode/generation identity.

| Crash point | Create recovery | Replace recovery |
|---|---|---|
| desired generation not durable | do not call exchange | do not cancel current protection |
| desired durable before claim | claim and process | verify old and new generation policy |
| during query/cancel/create | mark UNKNOWN; query current-generation orders | query both generations and every known alias |
| create ACK before active verification | `EXCHANGE_ACKED`, not `ACTIVE` | replacement not complete |
| verified active before alias writeback | generation-fenced local projection | bind new alias; preserve observed old disposition |
| alias/projection complete before operation completion | complete journal only | complete journal only |
| restart with stale generation | no exchange mutation | stale worker cannot cancel or recreate anything |

Replacement must choose a product policy for overlap versus protection gap. A
generic transaction cannot atomically cancel one exchange algo order and create
another. Recovery therefore records each observed effect and never infers that
cancel failure means old protection remains active.

## 19. Ghost Finalization Crash Matrix

`GHOST_FINALIZE` starts from exchange evidence that an exact tracked episode is
flat. It does not submit a close by default.

Candidate order:

```text
durable ghost operation + observed-flat evidence
-> acquire episode/generation lease
-> authoritative final accounting ACK
-> exact-episode projection removal ACK
-> episode tombstone/final stage ACK
```

| Crash point | Required recovery |
|---|---|
| before operation intent | preserve local metadata; rescan may rediscover ghost |
| after intent before accounting | query that the same episode/slot is still flat, then record |
| after accounting before removal | dedup by episode/operation and remove exact old projection |
| after removal before tombstone/completion | recover from journal/accounting evidence; never depend on removed metadata alone |
| after tombstone before return | return completed prior result |
| slot reopened as episode B | episode A finalizer may finish accounting/tombstone but must not remove or mark B |

Current `ghost_cleanup_one` does `positions.pop` before `record_trade`, and
`reconcile_all` can silently pop with no record, marker, or lock. Both orderings
are incompatible with this candidate required-finalization model.

## 20. External Reconciliation Crash Matrix

`EXTERNAL_RECONCILE` separates physical discovery from notification and product
disposition.

| Stage | Durable requirement | Recovery behavior |
|---|---|---|
| observe exchange-only exposure | snapshot/evidence plus slot key | repeat query; do not call it a new local open |
| classify lineage | bind known request/episode aliases or mark unknown ownership | quarantine ambiguity |
| choose disposition | approved adopt, internal repair, business close, or quarantine policy | no implicit policy from process restart |
| project/adopt | allocate approved episode/generation and CAS local state | idempotent projection |
| notify/audit | independent delivery/event state | failure must not undo physical classification/projection unless explicitly required |
| complete | required disposition acknowledged | auxiliary delivery may remain pending |

The current pending/seen notification keys do not own this operation. `seen`
does not prove Telegram delivery, PG commit, or local adoption.

## 21. Local Projection And CAS

Future position writes must stop using an unversioned read-modify-write snapshot
as a commit primitive. A candidate projection write compares:

```text
exchange_position_key
expected position_episode_id
expected lifecycle_generation
expected projection_revision
operation_id applying the change
```

The result must distinguish:

- applied and acknowledged;
- already applied by the same operation;
- rejected because a newer episode/generation exists;
- unavailable before attempt;
- UNKNOWN because commit acknowledgement was lost.

Whole-snapshot storage can remain an implementation detail only if an atomic
adapter supplies equivalent per-slot compare-and-swap and acknowledgement.

## 22. Required Versus Auxiliary Completion

D4 policy is applied per stage:

| Work | Candidate role |
|---|---|
| operation intent/stage/identity | `REQUIRED` recovery state |
| exchange order/protection query evidence | `REQUIRED` for resolving ambiguity |
| managed position projection | `REQUIRED` for local lifecycle completion |
| authoritative close/partial accounting | `REQUIRED`, pending product sink decision |
| current-generation protection desired state | `REQUIRED` after accepted exposure |
| ClickHouse analytics | normally `BEST_EFFORT` or `DERIVED` |
| Telegram/logging | `BEST_EFFORT` unless separate delivery SLO is approved |
| disposable caches | `CACHE_ONLY` |

A required write failure before exchange mutation blocks that mutation. A
required write failure after physical effect creates recovery-required state.
Best-effort failure must not hold the core operation open silently.

`AUXILIARY_PENDING` is valid only for explicitly durable retryable auxiliary
delivery. Fire-and-forget work may be logged as lost but cannot pretend to be a
recoverable pending stage.

## 23. Degraded Modes

| Failure | Candidate safe mode | Prohibited inference/action |
|---|---|---|
| journal authority unavailable | block new opens and non-emergency mutations that cannot first persist intent; continue observation | do not submit then hope to reconstruct |
| local projection store unavailable | block new opens before submit; after effect, retain recovery state and query exposure | do not return ordinary failure implying no effect |
| accounting authority unavailable | keep close/ghost finalization pending; product decides emergency close admission | do not duplicate accounting or restore exposure |
| exchange mutation API unavailable | safe retry only before any call; otherwise UNKNOWN/query pending | do not assume rejected |
| exchange query API unavailable | retain UNKNOWN and current lease/retry scheduling | do not resubmit on timeout |
| protection worker unhealthy | durable work remains pending; block/admit/compensate according to open SLO policy | do not call queue insertion PROTECTED |
| notification/analytics unavailable | complete core and record explicit auxiliary loss/pending policy | do not roll back physical lifecycle state |
| conflicting journal/projection/accounting evidence | quarantine slot and alert | do not pick the latest wall clock blindly |

Emergency risk-reducing close while the journal is unavailable is a product
decision. If approved, it needs a separately auditable break-glass identity and
must be reconciled later; it is not normal operation replay.

## 24. Consistency Guarantees By Operation

The target is not global exactly-once execution.

| Operation | Candidate guarantee |
|---|---|
| `OPEN` | one logical request maps to one recoverable operation; no blind duplicate submit; exchange ambiguity is reconciled; projection is eventually idempotent |
| `CLOSE_FULL` | no blind duplicate reduction; confirmed flat is eventually finalized once per episode identity |
| `CLOSE_PARTIAL` | each discovered fill slice is accounted at least once operationally and deduplicated by fill/event identity; remaining projection converges to exchange truth |
| `PROTECTION_CREATE` | current desired generation is eventually verified active or escalated/compensated within selected policy |
| `PROTECTION_REPLACE` | old/new dispositions are reconciled with generation fencing; stale work cannot own current protection |
| `GHOST_FINALIZE` | exact flat episode is eventually accounted and removed without deleting a later episode |
| `EXTERNAL_RECONCILE` | observed exposure is eventually adopted, quarantined, or acted on under explicit policy; notification is independent |

The implementation may provide exactly-once *logical projection/accounting* by
idempotent keys. It cannot claim exactly-once physical exchange effects without
an exchange-supported stable identity plus complete query semantics.

## 25. Compatibility Result Model

Internal orchestration needs a structured result distinct from current legacy
bool/None surfaces:

| Result | Meaning |
|---|---|
| `SUCCESS` | selected required completion point is acknowledged |
| `ACCEPTED_PENDING` | physical/core progress is known and required follow-up remains durably recoverable |
| `UNKNOWN` | exchange or required-write outcome is ambiguous; caller must not retry as a new request |
| `FAILED` | terminal or retryable failure is proven before the represented effect, with category attached |

Candidate fields:

```text
status
operation_id
request_id?
position_episode_id?
stage
effect_summary?
retry_after?
error_category?
```

Legacy mappings cannot be selected safely in design alone:

- current open `False` can mean gate rejection, exchange failure, parser error,
  or post-fill local persistence failure;
- full close `False` can mean no-fill, partial fill, exception, or duplicate
  suppression;
- `partial_close` returns `None` for both success and failure;
- `OrderExecution.rejected` deliberately preserves `None`, code, or `True`.

D3E must introduce the structured internal contract and an explicit product-
approved compatibility adapter. `UNKNOWN` must never map to a value documented
as “no exchange effect occurred.”

## 26. Storage Options

### Redis authority

Strengths:

- low-latency atomic scripts/CAS and lease primitives;
- already operationally present.

Requirements/risks:

- persistence, replication, failover, eviction, and retention guarantees must
  be explicit;
- current wrappers swallow errors and provide no ACK;
- whole snapshots and symbol timestamp markers are insufficient;
- historical evidence and operational querying are harder.

### PostgreSQL authority

Strengths:

- transactional operation/current-stage/attempt/outbox tables;
- uniqueness, row revision, durable query, and retention are natural;
- can atomically couple journal stage with PG-owned accounting/outbox writes.

Requirements/risks:

- runtime currently treats PG as optional write-only history;
- availability/latency becomes an open/close admission dependency;
- it still cannot transact atomically with Binance or Redis.

### Hybrid authority

A hybrid may use one durable source of truth and a derived cache/lease, for
example PG journal plus Redis wakeup/lease. It must name one conflict winner.
Dual authoritative stage records create a second distributed commit problem and
are rejected unless a proven replication protocol defines acknowledgement and
recovery.

No backend is selected by P10-D3. Product/operations must approve durability,
latency, retention, recovery point, and degraded-mode requirements first.

## 27. Observability And Operator Controls

Required operational signals include:

- nonterminal operation count and oldest age by type/stage;
- UNKNOWN count, age, query failures, and evidence conflicts;
- lease takeover and stale-fence rejection counts;
- fill-to-projection and fill-to-protection duration;
- flat-to-accounting/finalization duration;
- compensation-required and compensation-UNKNOWN operations;
- degraded admission state by dependency;
- generation mismatch/quarantine events;
- auxiliary pending/loss counts separated from core failure.

Operator actions must be identity-bound and audited: retry safe stage, refresh
evidence, approve compensation, quarantine, or declare terminal after product
criteria. “Clear Redis key and retry” is not a valid recovery protocol.

## 28. End-To-End Recovery Scenarios

### Open fills, process dies before local state

1. Journal shows `SUBMITTING` for request R and operation O.
2. Recovery queries the stable exchange client identity.
3. Fill evidence proves exposure.
4. O moves to `EFFECT_CONFIRMED`; no second open is submitted.
5. Episode E is bound, local projection is CAS-written, open event is keyed,
   and protection generation 1 is durably scheduled.
6. Result is `ACCEPTED_PENDING` or `SUCCESS` according to protection policy.

### Close fills, PG is unavailable

1. Operation C has confirmed flat evidence.
2. C remains `EFFECT_CONFIRMED` with accounting/finalization pending.
3. Restart does not submit another close.
4. Accounting retries by operation/fill identity.
5. Exact episode projection is removed and tombstoned after required ACKs.

### Protection create times out

1. Generation G is `SUBMITTING` and create outcome is ambiguous.
2. It becomes `UNKNOWN`; no second create is sent.
3. Recovery queries known aliases/current algo orders and matches episode, side,
   trigger, quantity, and generation.
4. A match transitions to `ACTIVE`; proven absence permits a same-generation
   retry; conflict quarantines/escalates.

### Ghost episode A races reopen episode B

1. Ghost finalizer carries episode A and lifecycle generation A.
2. Slot now projects episode B/generation B.
3. A may finish idempotent accounting/tombstone for A.
4. CAS rejects removal or marker mutation against B.

## 29. Product Decisions And Implementation Gates

The following are unresolved and block behavior implementation:

1. Does open `SUCCESS` require verified protection or durable pending work?
2. What fill-to-protection SLO and failure action apply?
3. What UNKNOWN timeout/escalation applies to each exchange mutation?
4. Which backend is the authoritative operation journal?
5. Which sink is authoritative for close and partial accounting?
6. Is duplicate avoidance preferred over bounded loss/latency in ambiguous
   close/open cases?
7. When is automatic compensation close required or forbidden?
8. Which operations are admitted in each degraded mode?
9. How are legacy bool/None callers migrated and mapped?
10. What are scale-in, side-flip, external ownership, and episode boundaries?
11. Is break-glass close allowed without journal availability?

No D3 implementation split is production-ready until its named dependencies
and decisions are satisfied.

## 30. T12 Mapping And Implementation Split

P10-D3 supersedes the T12 design ticket, not the missing runtime behavior:

| T12 area | D3 disposition |
|---|---|
| T12-A open crash consistency | operation/request identity, O1-O8, UNKNOWN, protection handoff |
| T12-B close/reconcile consistency | C1-C7, partial, ghost, external reconciliation |
| T12-C persistence/partial commit | D4 policy integrated with commit points, ACK, CAS, and replay |
| T12-D restart recovery | coordinator, nonterminal scan, leases, fencing, evidence resolution |

`T12 = SUPERSEDED_BY_D3_DESIGN` means design consolidation is complete. It does
not mean operation journaling, restart recovery, or crash behavior exists.

Suggested implementation split:

| ID | Scope | Readiness |
|---|---|---|
| P10-D3A | operation journal schema, authority, CAS, lease, and retention | `BLOCKED_BY_PRODUCT_DECISION` |
| P10-D3B | startup/continuous recovery coordinator and idempotent stage replay | `BLOCKED_BY_D3A_D3C_D3D` |
| P10-D3C | exchange UNKNOWN resolver for order/fill/position/algo evidence | `BLOCKED_BY_D1_D2_D4_D5_AND_PRODUCT_DECISION` |
| P10-D3D | episode/lifecycle/protection generation fencing and projection CAS | `BLOCKED_BY_D2_D5_AND_D3A` |
| P10-D3E | structured result and legacy bool/None compatibility adapter | `BLOCKED_BY_PRODUCT_DECISION` |

PMB-23A remains blocked by the authoritative close sink and duplicate-versus-
loss decision. PMB-26C2 now has a staged design but remains blocked by C1/B2
policy decisions and D3 implementation prerequisites. PMB-24 remains a product
policy ticket.

## 31. Audit Result

| Requirement | Result |
|---|---|
| unified operation taxonomy | PASS |
| explicit state machine and commit points | PASS |
| operation/request/episode/slot/generation identity separation | PASS |
| O1-O8 open crash windows | PASS |
| C1-C7 full-close crash windows | PASS |
| partial/protection/ghost/external crash analysis | PASS |
| restart, ownership, CAS, generation fencing, UNKNOWN resolution | PASS |
| replay/compensation and degraded-mode policy | PASS |
| consistency guarantees and compatibility result model | PASS |
| Redis/PG/hybrid storage tradeoff | PASS |
| T12 mapping and D3A-D3E readiness | PASS |
| production behavior changed | NO |

**P10-05 PASS.**

P10-D3 is **DESIGN AUDITED**. The audit defines the integrated crash/restart
contract and identifies implementation prerequisites. It does not authorize a
production change, and no D3 implementation ticket is currently ready.
