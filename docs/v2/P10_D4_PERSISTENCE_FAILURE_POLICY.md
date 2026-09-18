# P10-04 / P10-D4 - Persistence Failure Policy

> Architecture and semantic design only. Production code is unchanged. This
> document classifies persistence roles, acknowledgement, failure, retry, and
> recovery semantics before exception topology or write ordering is changed.

## 1. Scope And Guardrails

P10-D4 asks:

```text
After this write fails, does the system still know what happened in the real
world, and can the next attempt recover without repeating an unsafe effect?
```

It does not authorize:

- changing PG, Redis, file, ClickHouse, Telegram, or marker helpers;
- changing any current swallow/propagate boundary;
- adding a retry, outbox, journal, transaction, or acknowledgement;
- changing open, close, reconcile, ghost, notification, or protection behavior;
- implementing PMB-23A, PMB-26C1/C2, T12, or D3.

## 2. Policy Vocabulary

Every future persistence action must have one primary policy and any necessary
outcome modifiers:

| Policy | Meaning | Business gating |
|---|---|---|
| `REQUIRED` | The workflow cannot declare its semantic stage complete without acknowledged persistence | failure before an irreversible effect blocks submission; failure after it creates incomplete recovery-required state |
| `BEST_EFFORT` | Failure does not change the business effect or stage | log/metric and continue; loss is explicitly accepted |
| `RETRYABLE` | Durable intent remains and repeating this write by the same identity is safe | retry the write, not the business operation |
| `UNKNOWN` | The write may or may not have committed | query/reconcile by identity before retry; do not assume failed or successful |
| `DERIVED` | Data can be rebuilt from a named authoritative source | loss may trigger rebuild; source and reconstruction bounds must be explicit |
| `CACHE_ONLY` | Disposable acceleration with no correctness or recovery role | loss is acceptable and no business stage waits for it |

`COMPENSATABLE` is a response category, not a persistence guarantee. It means a
separate business effect can reduce risk after required progress fails, such as
closing an unprotected position. Compensation is not rollback and can itself be
`UNKNOWN`.

### Required write after an irreversible effect

If Binance has already filled an order, a later required persistence failure
cannot make the fill disappear. The workflow must not return an ordinary
failure that implies no effect. It must retain or reconstruct an incomplete
stage such as `FILLED_STATE_PENDING` or `FLAT_FINALIZATION_PENDING` and enter
recovery. This is the key input to D3.

## 3. Persistence Role Taxonomy

Roles describe why data exists; they are independent of database technology:

| Role | Definition |
|---|---|
| A. Operational Source | current state used to make live decisions or drive work |
| B. Recovery Source | durable state read after failure/restart to resume or reconstruct |
| C. Audit Ledger | immutable/idempotent evidence of observed business/exchange events |
| D. Historical Analytics | history used for reporting, analysis, or derived strategy signals |
| E. Coordination State | leases, claims, locks, generations, and ownership fencing |
| F. Notification State | claim, attempt, delivery acknowledgement, retry, or escalation status |
| G. Dedup/Tombstone State | suppression or finalized-episode memory |
| H. Disposable Cache | rebuildable process/runtime acceleration |

A sink may serve multiple declared roles, but one write must not silently change
role by caller. For example, Redis can host operational state and a lock, but a
best-effort snapshot write is not automatically a durable lease.

## 4. Source Of Truth Is Not A Write Destination

Writing to a database does not make it authoritative. Authority requires:

1. a defined fact or stage owned by the sink;
2. an acknowledgement contract;
3. readers that use it for decisions or recovery;
4. conflict and replay rules;
5. retention sufficient for the promised recovery window.

Current operational model:

```text
Binance = physical exposure/order truth
Redis/file = operational metadata and coordination attempts
PG = write-only event/episode history for the trading runtime
ClickHouse = historical/derived analytics and advisory strategy input
process memory = disposable queues/caches
```

PG `trade_events` has no runtime recovery reader. Its deterministic keys and
transactional insert do not by themselves make it runtime truth. Conversely,
ClickHouse is not a lifecycle truth source even though `trade_analysis` can
influence future open admission and sizing.

## 5. Sink Inventory

| Sink/state | Writers | Live readers | Recovery reader | Current failure behavior | Durability purpose |
|---|---|---|---|---|---|
| Redis `pm:positions` | active executor, state/lifecycle/monitor/reconcile, protection writeback | PM and strategies | exchange/Redis/file load chain | whole-snapshot save has no acknowledgement; failures swallowed | operational position metadata |
| Local mapped JSON state/file | `redis_store.set/delete` after Redis attempt | Redis fallback helper | fallback only when mapping resolves to an existing file | direct overwrite, errors logged/swallowed, no fsync/atomic rename/ack | secondary fallback/recovery copy |
| Redis `closed:{symbol}` | close/ghost marker paths | open, close, state filter, ghost, self-heal | none beyond Redis value | SET/GET/DELETE failures collapse to absent/no-op; no file fallback | dedup/tombstone/cooldown/weak coordination |
| Redis notification pending | external-position notifier/merge | notifier | none | write result unavailable; failure can restart grace forever | notification grace state |
| Redis notification seen | external-position notifier | notifier | none | written before delivery; failure/result unacknowledged | notification attempt suppression |
| Redis partial accumulator | close recorder | final settlement | final close only | write/clear unacknowledged; no file fallback | partial-close accounting staging |
| PG `trade_events` | active open, close branches, external detection, offline fill reconciler | reports only | no lifecycle recovery reader | disabled/DB errors return `False`; preparation can raise; most runtime callers ignore result | audit/event ledger candidate |
| PG `trade_episodes` | final settlement | reports/attribution | no lifecycle recovery reader | upsert returns bool but recorder discards it; exceptions can be swallowed by ledger service | position-level close/accounting ledger candidate |
| ClickHouse `trade_history` | close recorder and legacy paths | dedup, reports, metrics, analyzer replay | limited analytics replay, not lifecycle recovery | queries return empty on error; inserts raise into swallowing callers | historical analytics and heuristic dedup |
| ClickHouse `trade_analysis` | analyzer worker | active open analysis gate and reports | bounded history rebuild only | query failures look like no history; async work is memory-backed | derived advisory strategy input |
| Protection task/state | in-memory `_ALGO_QUEUE`; local `sl`/`algo_sl_id` hints | worker/monitor | no task replay | task lost on restart, one attempt, no requeue | disposable current work; future desired-state candidate |
| Future operation journal | none | none | none | absent | candidate recovery truth for incomplete operations |

## 6. Current Role Assignment

| Sink/state | Current role(s) | Not currently proven to be |
|---|---|---|
| `pm:positions` | A Operational Source; partial B Recovery Source | physical exposure truth; transaction journal |
| mapped local state file | B Recovery Source candidate | equivalent synchronous replica; durable ACK |
| closed marker | E Coordination attempt; G Dedup/Tombstone | episode-safe lease or lifecycle truth |
| pending/seen | F Notification State | position state or delivery acknowledgement |
| partial accumulator | C accounting staging; B limited final-close input | complete immutable fill ledger |
| PG events | C Audit Ledger candidate | runtime operational/recovery source |
| PG episodes | C close/accounting ledger candidate | proven required financial authority |
| CH history | D Historical Analytics; heuristic dedup input | lifecycle recovery truth |
| CH analysis | D Derived Analytics/advisory input | authoritative risk/position state |
| process queues/caches | H Disposable Cache/work | durable handoff |
| future operation journal | B Recovery Source; E operation ownership/stage | exchange exposure truth |

## 7. Redis Operational Position State

### Current facts

- `pm:positions` is a top-level symbol dictionary.
- Saves overwrite the complete snapshot.
- Multiple writers perform independent read-modify-write cycles.
- There is no per-symbol CAS, schema revision, or shared transaction.
- `RedisPositionStateAdapter.save_positions` catches all exceptions.
- `PositionStateService.save` catches again and returns `None`.
- Active `_update_pos_cache` also catches direct Redis failures and still returns
  its generated `position_id`.
- Restart load prefers fresh exchange WS, then exchange REST, then local
  metadata; successful exchange reconstruction loses original lineage when
  metadata is unavailable.

### Should save failure fail the business operation?

Candidate D4 policy:

**Managed position metadata is `REQUIRED` operational persistence for declaring
the local lifecycle stage complete, but it is not physical exchange truth.**

The timing matters:

| Failure point | Candidate semantic result |
|---|---|
| Required state/intention write before exchange submit | block submit; no business effect should occur |
| State write after confirmed fill | do not claim "no open"; enter `FILLED_STATE_PENDING` / recovery-required |
| State removal write after confirmed flat | physical close succeeded; enter `FLAT_FINALIZATION_PENDING` rather than ordinary completed close |
| Advisory/cache write | continue under explicit `CACHE_ONLY` policy |

Exchange snapshots can recover physical side/quantity/entry, but not request,
episode, owner, protection generation, or pending finalization. This prevents
classifying current silent state loss as harmless `DERIVED` data.

Product must approve whether open success requires acknowledged local state and
whether degraded operation may hold exposure while operational persistence is
unavailable.

## 8. File Fallback Policy

### Current behavior

`redis_store.set` performs:

```text
JSON encode
-> Redis SET attempt
-> mapped file write attempt when double_write=True
-> return None
```

The file path is usable only when the mapped file already exists. Dynamic keys
have no mapping. File writes use direct `Path.write_text` with no temporary
file, atomic rename, lock, checksum, or `fsync` acknowledgement.

| Outcome | Current result |
|---|---|
| Redis success, file failure | Redis is newer; warning only; caller cannot detect degraded fallback |
| Redis failure, file success | local file may be newer, but other processes see stale/missing Redis; caller cannot detect split state |
| Both fail | caller usually continues as though save was invoked successfully |
| Redis later has any value | Redis wins even if fallback file contains a newer failed-write snapshot |
| Redis key absent and file exists | helper backfills Redis from file |

### Candidate policy

The current file is a `BEST_EFFORT` secondary recovery copy, not an equivalent
acknowledged replica. It cannot satisfy a `REQUIRED` position-state write until
the contract defines creation, atomic replacement, flush durability, version,
conflict winner, and acknowledgement. Redis ACK and file ACK must be reported
separately if dual durability is required.

## 9. Marker Persistence Policy

Current marker persistence is Redis-only, symbol-scoped, timestamp-based, and
unacknowledged.

| Failure | Current behavior |
|---|---|
| Marker SET fails during close | close continues without reliable cross-process dedup/cooldown |
| Marker GET fails during open/close | acts absent; open/close may proceed |
| Marker DELETE fails after no-fill/partial/error | stale fresh marker may continue blocking |
| Self-heal DELETE fails | exchange position remains visible but marker may keep affecting other readers |
| Ghost lock fails | cleanup is skipped, unlike marker failure which often fails open |

Candidate policy:

- The current marker is not allowed to become the business commit gate because
  it lacks episode identity and acknowledgement.
- Failure is currently `BEST_EFFORT` with degraded dedup/recovery and must be
  observable.
- A future typed close lease/generation may be `REQUIRED` coordination for
  exclusive finalization, while emergency risk-reducing exchange close remains
  separately admissible by product policy.
- Episode tombstone/cooldown writes need their own policy; inability to persist
  cooldown must not rewrite physical close truth.

## 10. Notification-State Policy

Pending and seen are notification state, not position state.

Current external detection order is:

```text
pending/grace checks
-> write seen
-> clear pending
-> log
-> Telegram attempt
-> PG event attempt
-> merged position assignment/save
```

`seen` currently means "attempt authorized/suppressed," not Telegram delivered,
PG committed, or local adoption completed. Telegram status/body is not validated
and exceptions are swallowed.

Candidate policy:

- Position reconciliation should not be blocked by `BEST_EFFORT` notification
  delivery unless product explicitly makes operator acknowledgement a safety
  gate.
- If product promises at-least-once/bounded delivery, notification operation
  state becomes `REQUIRED + RETRYABLE` for the notification subsystem, but
  remains separate from position adoption and event audit state.
- Pending, claim, attempted, delivered, failed, and escalated must be separate
  meanings. A single `seen` timestamp cannot acknowledge all of them.

## 11. PostgreSQL Event Ledger Policy

### Current helper acknowledgement

`record_trade_event(data)`:

- returns `False` when PG is disabled;
- uses one PG transaction and `ON CONFLICT(event_id) DO NOTHING`;
- returns `False` for connection/SQL/commit exceptions caught by the helper;
- can propagate argument/JSON preparation errors before its `try`;
- returns `True` for both a new insert and a duplicate no-op;
- has no caller retry in active runtime paths;
- has no runtime recovery reader.

Active open, close, and external-detection callers normally ignore `False`.
Exception topology differs by call site and can abort work after exchange or
Redis side effects already committed.

### Candidate policy and PMB-26C1 fork

Repository behavior supports this recommended candidate:

**PG `trade_events` is an explicit `BEST_EFFORT` audit ledger with mandatory
failure metrics/logging, unless product upgrades it to required durability.**

The unresolved alternatives are:

| Product choice | Correct consequence |
|---|---|
| Event ledger is `BEST_EFFORT` | a raised preparation/callable error should eventually be isolated consistently; ignored `False` is semantically acceptable only with explicit observability/loss budget |
| Event ledger is `REQUIRED` | ignored `False` is the primary bug; the operation needs durable pending state/retry and must not silently finalize without event acknowledgement |

Changing exception handling before choosing one would optimize the wrong
contract. PMB-26C1 therefore remains product-blocked.

## 12. PostgreSQL Episode/Close Ledger Policy

Episode accounting is not the same role as event audit.

Current settlement:

```text
partial accumulator / PnL work
-> PG trade_episodes upsert
-> ClickHouse trade_history insert
-> analysis enqueue
-> stats query / Telegram
```

`upsert_trade_episode` returns a boolean, but the recorder shim discards it and
the ledger service does not inspect it. A `False` permits CH and later work. A
preparation exception can be swallowed by the ledger service while skipping CH
and analysis. The lifecycle caller receives no structured settlement ACK.

Candidate policy:

- A single authoritative close/accounting finalization record is likely
  `REQUIRED + RETRYABLE`, but product must select the sink and duplicate-versus-
  loss preference.
- PG `trade_episodes` is a candidate because it upserts by `position_id`, but
  current optional configuration, discarded result, identity limitations, and
  lack of recovery reader do not yet prove that role.
- Physical exchange flatness cannot be rolled back if accounting fails. The
  result must be `FLAT_FINALIZATION_PENDING`, not "close did not happen."
- Local state removal should not destroy the final replay source until the
  required finalization intent/record is acknowledged.

This decision blocks PMB-23A and T12 close finalization.

## 13. ClickHouse Policy

ClickHouse has three current roles:

1. `trade_history` historical rows and reports;
2. heuristic query-before-insert close-record dedup/statistics;
3. `trade_analysis` derived rollups that can block or reduce future active opens.

It has no reader that restores position episode, open/close operation stage, or
protection after restart. Therefore it is not a lifecycle recovery source.

Current failures:

- query errors become empty results;
- historical dedup then fails open;
- analysis-history failure appears as insufficient history and active open gate
  allows trading;
- inserts raise, but recorder/analyzer callers commonly catch/drop them;
- `trade_history` has no repository-proven unique idempotency constraint;
- analysis work uses process-local queues with only limited history replay.

Candidate policy:

**CH history/analysis writes are `BEST_EFFORT`; rollups are `DERIVED` advisory
inputs with explicit degraded/fail-open behavior.**

If product wants historical quality gates to be mandatory risk controls, their
availability policy must be separately promoted. That still would not make CH
position or operation truth.

## 14. Protection State Policy

Current protection tasks are process-memory `CACHE_ONLY` work despite carrying
safety-critical intent. They are lost on restart and have no retry, generation,
or post-ACK verification. Local `sl` and `algo_sl_id` are hints, not a durable
pending operation.

D2 requires future `PROTECTION_PENDING` desired state to be:

- `REQUIRED` after confirmed fill if protection is part of the approved open
  safety contract;
- `RETRYABLE` by stable episode/generation identity;
- `UNKNOWN` after ambiguous exchange create/cancel acknowledgement;
- recoverable across restart before disposable worker delivery.

The current queue must not be reclassified as durable merely because it is
called a handoff.

## 15. Acknowledgement Semantics

| Observation | What it proves | What it does not prove |
|---|---|---|
| Python function returns | control returned | persistence, durability, insertion, delivery, or cross-sink agreement |
| Redis command response | server accepted the command in that connection | file copy, disk persistence policy, replica durability, or later LWW correctness |
| Current Redis helper returns `None` | nothing about either destination | Redis or file success |
| File `write_text` returns | bytes were passed through OS write path | `fsync`, atomic replacement, crash survival, replica agreement |
| PG helper returns `True` | transaction context completed without observed exception | row was newly inserted; another sink committed |
| PG helper returns `False` | disabled or observed failure | definite non-commit under commit-response ambiguity |
| CH insert returns | command did not raise | lifecycle recovery or deduplicated insertion |
| TG request returns | HTTP call returned | response accepted/delivered unless status/body is validated |
| Exchange order ACK | exchange response was observed | local persistence, audit, protection, or global exactly-once |

Future acknowledgements must name level: accepted by process, accepted by
server, transaction committed, replicated/durable by configured policy, or
business stage persisted.

## 16. Active Open Partial-Commit Matrix

| Exchange/business fact | Persistence failure | Current result | Retry/recovery risk | Candidate policy |
|---|---|---|---|---|
| MARKET may be accepted but response absent | no request/outcome record | open returns `False` | blind retry can duplicate exposure | `UNKNOWN`; query exchange by stable request identity |
| Fill confirmed | local state save fails silently | open continues, may return `True` | lineage/owner/protection recovery lost | required state/journal stage; recovery-required |
| Local state published | PG event returns `False` | ignored | audit gap; no retry | best-effort candidate or required pending event by product |
| Local state published | PG event raises | outer path returns `False`; TG/protection skipped | real exposure may be treated as rejected and left unprotected | never collapse post-fill failure into ordinary rejection |
| State/PG complete | TG callback raises | outer path can return `False`; protection skipped | optional side effect controls safety ordering | notification should be separate unless explicitly required |
| State/TG complete | enqueue fails | logged; open returns `True` | no durable protection intent | required D2 desired state and failure action |
| Algo order accepted | `algo_sl_id` save fails | exchange may be protected, alias absent | later cancellation/restart ambiguity | exchange truth plus retryable alias repair |

Open success cannot be one boolean across these stages. D3 must represent at
least exchange outcome, local episode publication, protection state, and
optional fan-out independently.

## 17. Close Partial-Commit Matrix

| Exchange/business fact | Persistence failure | Current result | Retry/recovery risk | Candidate policy |
|---|---|---|---|---|
| Close intent begins | marker write fails | exchange close still proceeds | duplicate close/finalization coordination degraded | marker not physical gate; future required typed operation lease |
| Close ACK ambiguous | marker later clears on exception | returns `False` | retry may repeat unknown exchange effect | `CLOSE_UNKNOWN`; exchange query before resubmit |
| Partial fill | remaining state save fails | accounting continues | local quantity stale; exchange remains truth | durable close stage/fills; state projection retry |
| Partial fill | Redis partial accumulator fails | PG partial may continue | final accounting omits slice | select required fill/accounting source |
| Exchange flat | PG event returns `False` | ignored | event missing; recorder/pop proceed | best-effort event candidate or required outbox by product |
| Exchange flat | PG event raises | branch-dependent abort after close | local state/marker/ledger divergence | durable finalization stage before retirement |
| Episode PG fails | CH/TG may continue | no structured error | split historical truth | one authoritative required ledger, downstream best effort |
| PG succeeds, CH fails | caller still removes state/returns | analytics gap | retry may be duplicate-prone | best-effort CH with idempotent downstream identity |
| Ledgers attempted | local pop/save fails | close can return `True` | stale local position can trigger ghost/replay | required state transition acknowledgement/reconcile |

## 18. Reconcile, Ghost, And Notification Matrix

| Flow | Side effect before failure | Failed write | Current behavior | Policy question |
|---|---|---|---|---|
| Direct reconcile | local-only entries popped in memory | whole snapshot save | failure swallowed; result still reports ghosts | is prune administrative repair, close, or quarantine? |
| Ghost cleanup | position popped before recorder | recorder/ledger | raising error logged; later save may remove replay source | is accounting required before destructive removal? |
| Ghost cleanup | recorder returns after silent sink loss | PG/CH/TG | marker/removal proceed as success | recorder attempt is not durable ACK |
| Ghost cleanup | record committed | marker write | failure swallowed | duplicate accounting window remains |
| External detection | seen/pending updated, TG attempted | PG event returns `False` | merge continues; no retry | event best effort or required? |
| External detection | same prefix | PG raises | current/later symbols and snapshot save abort | C1 policy then C2 isolation/retry design |
| Notification | seen written | TG delivery fails | exception swallowed; 24h suppression | what does seen acknowledge? |

## 19. PMB-23A Dependency

Current ghost ordering is:

```text
pop local in-memory source
-> record_trade fan-out
-> mark closed
-> later whole-snapshot save
```

If recording is `BEST_EFFORT`, moving record before pop does not create a
durability guarantee and may only change duplicate/loss timing. If close
accounting is `REQUIRED`, pop-before-ack can destroy the only automatic replay
source and is unsafe.

PMB-23A therefore remains `BLOCKED_BY_PRODUCT_DECISION`: product must select the
authoritative close record and duplicate-versus-loss preference. Its integrated
ordering/replay implementation then belongs to D3.

## 20. PMB-26C1 And C2

### PMB-26C1

Two coherent policies exist:

```text
BEST_EFFORT event audit:
  failure is isolated, measured, and accepted;
  event does not gate reconcile/business completion.

REQUIRED event durability:
  False/UNKNOWN must be retained and retried;
  completion cannot silently advance past the missing event.
```

Current code mixes them: ordinary DB failures become ignored `False`, while
some preparation/callable exceptions propagate. D4 does not select product
durability and does not change this topology.

### PMB-26C2

Batch isolation, marker/seen rollback, retry, and compensation depend on C1. If
the event is best effort, rollback is usually wrong. If it is required, a
durable staged operation is needed because already committed TG/Redis/exchange
effects cannot be rolled back atomically.

PMB-26C2 is therefore `BLOCKED_BY_D3`, with prerequisite product decisions for
PMB-26C1 and PMB-26B2.

## 21. Retry Safety Matrix

| Write/effect | Retry classification | Reason |
|---|---|---|
| Redis same snapshot SET | superficially idempotent | stale full-snapshot replay can overwrite newer unrelated updates |
| Local file overwrite | superficially idempotent | no version/CAS; stale copy can replace newer state |
| Marker SET | not idempotent in policy time | retry writes a new timestamp and extends cooldown |
| Marker DELETE | logically idempotent | blind delete can remove a newer episode/operation marker |
| Pending/seen SET | payload-dependent | timestamp/fingerprint changes alter grace and suppression |
| PG event insert | idempotent by exact `event_id` at DB | identifier scope/collision and commit ambiguity still matter |
| PG episode upsert | convergent only for one correct episode ID | retries update close fields/time; ID collision merges episodes |
| Redis partial accumulator | duplicate-prone | repeating a slice adds quantity/PnL again |
| CH `trade_history` insert | duplicate-prone/unknown | no proven unique key; heuristic pre-query races |
| CH analysis insert | heuristic dedup | query-before-insert and memory replay can race |
| Telegram send | duplicate-prone | no durable delivery idempotency key/ack state |
| Exchange order submit | unsafe after ambiguous ACK | can duplicate physical effect |
| Protection create/cancel | unsafe without query/generation | can leave zero or duplicate protections |

Retry safety comes from stable identity, durable stage, sink idempotency, and
outcome query. A retry loop alone provides none of these.

## 22. Failure-Domain Matrix

| Failure | Blast radius | Current recoverability | Recommended policy family |
|---|---|---|---|
| Redis down | state, marker, locks, notifications, partial accounting | exchange reconstruction plus limited existing mapped files; lineage lost | required operational writes enter degraded/recovery state; coordination failures alert |
| PG down | events and episodes | no runtime replay; exchange/Redis still trade | explicit best-effort audit or required durable pending/outbox by product |
| CH down | history, dedup, analysis and advisory open filter | queries fail open; limited analysis replay | best-effort/derived with degradation metrics |
| Disk full | mapped fallback and logs | Redis may remain; no file ACK | mark fallback degraded; do not count as durable replica |
| File write failure | secondary recovery copy stale | Redis if available | best-effort warning/metric; required only if policy explicitly needs dual durability |
| Serialization failure | can occur before helper try/write | caller-specific raise/abort | validation; classify operation as not persisted, preserve stage |
| Network timeout | sink may commit but response is lost | sink-specific query required | `UNKNOWN`, identity-directed reconciliation |
| Process crash after write-before-ack | committed prefix may survive; memory work lost | no operation journal today | durable stage/outbox and idempotent resume |
| Whole-snapshot race | unrelated state lost without exception | exchange can restore physical subset only | per-entity revision/CAS or journal projection |
| Redis/file divergence | different replicas hold different versions | no conflict resolver/version | explicit primary, version, repair, and separate ACKs |

## 23. Direct Write, Outbox, Journal, And Replay Options

| Model | Best fit | Strength | Limitation |
|---|---|---|---|
| A. Direct synchronous write | required local stage with trusted ACK; low-volume audit | simple immediate result | blocks path; cannot transact with Binance/other sinks |
| B. Best-effort async | disposable analytics/notifications under accepted loss | isolates latency/failure | memory-only form loses work and has no delivery guarantee |
| C. Transactional outbox | PG/CH/TG/event fan-out from one authoritative local commit | durable at-least-once delivery with idempotent consumers | atomic only with owning database; not Binance transaction |
| D. Durable operation journal | open/protect/close stages, UNKNOWN, restart resume | recovery lineage and ownership | requires authority, schema, stage machine, and reconciliation |
| E. Replay from exchange | exposure/order/fill repair | recovers physical facts | cannot reconstruct original request, owner, episode, notification, or desired protection alone |

Audit fan-out can use an outbox if audit becomes required. Notification can use
an outbox/durable delivery queue under at-least-once policy. Operation recovery
needs a journal, not merely an event sink. Exchange replay remains mandatory for
effects outside local transactions.

## 24. Exactly-Once Myth

No transaction spans Binance, Redis, PG, ClickHouse, Telegram, and files.
Exactly-once business effects cannot be obtained by wrapping one helper in a
transaction or moving one save.

The practical target is:

```text
stable operation/effect identity
+ durable intent and stage
+ idempotent or queryable sink effects
+ generation fencing
+ reconcile/replay after UNKNOWN/crash
+ explicit compensation where product requires it
```

At-least-once work plus idempotent effects is often appropriate. Physical
exchange submission after an ambiguous ACK must remain UNKNOWN, not be blindly
treated as an at-least-once retry.

## 25. Operation Journal Candidate

A future lifecycle journal needs at least:

```text
operation_id
operation_type
request_id
position_episode_id
exchange_position_key
stage
exchange_order_refs
exchange_fill_refs
desired_protection + protection_generation
attempt / owner / fencing metadata
last_error + outcome_class
created_at
updated_at
```

Optional downstream delivery statuses should be related records or explicit
substates, not one overloaded success flag.

The journal's authority is:

**incomplete operation lineage, ownership, and recovery stage.**

It is not exchange exposure truth. Recovery compares journal intent/stage with
exchange facts and operational state. A journal saying `SUBMITTED` cannot prove
that Binance accepted or rejected an order.

## 26. Ordering Constraints For D3

### Before irreversible exchange submission

- Persist a stable request/operation identity if retry/restart idempotency is
  required.
- Persist the desired operation and ownership/reservation before submit.
- If that required write is unacknowledged, do not submit.

### Immediately around exchange acknowledgement

- Mark submission outcome `UNKNOWN` whenever acceptance cannot be proven.
- Do not resubmit until identity-directed exchange reconciliation resolves it.
- Persist known exchange order/fill aliases before optional fan-out.

### After confirmed open fill

- Create/activate immutable episode lineage.
- Persist required operational state and durable desired protection.
- Hand off retryable protection before optional audit/notification can block
  safety progress.
- Declare open success only at the product-approved stage.

### After close submission/fills

- Persist close-operation progress and partial fills by stable identity.
- Confirm exchange flatness before declaring physical close.
- Persist required close-finalization/accounting intent before deleting the last
  local recovery source.
- Invalidate protection generation and use typed episode marker transitions.
- Deliver CH/TG/optional audit eventually under their selected policies.

These are constraints, not a production sequence patch.

## 27. Compensation Versus Retry

| Failure class | Correct response family |
|---|---|
| Known sink rejection before exchange action | retry required write or reject admission |
| Unknown sink commit | query sink by operation/effect identity before retry |
| Exchange ACK unknown | reconcile exchange; never retry persistence as a substitute |
| Operational projection missing after known fill | retry/rebuild projection; do not resubmit open |
| Protection deadline missed | product chooses hold/retry, compensate-close, quarantine, or halt opens |
| Exchange flat but ledger pending | retry finalization/accounting; do not submit another close merely to produce history |
| Optional audit/CH failure | log/metric, bounded retry if configured, continue business stage |
| Notification failure | retry delivery or escalate under B2 policy; do not repeat trade |
| Marker/coordination failure | degrade/alert or block ownership stage by policy; do not infer exchange outcome |

Retry the smallest failed idempotent effect. Compensate only when a product
safety policy asks for a new business effect.

## 28. Observability Contract

Minimum requirements by policy:

| Policy/outcome | Required observability |
|---|---|
| `REQUIRED` failure | structured error, operation/episode ID, sink, stage, alert, retry age/count, terminal escalation |
| `BEST_EFFORT` loss | counter by sink/event type, last success/failure, sampled logs, loss-rate alert budget |
| `RETRYABLE` | queue depth, oldest age, attempt count, next retry, dead-letter/escalation status |
| `UNKNOWN` | immediate high-severity state, query attempts/results, unresolved age, operator action |
| `DERIVED` degradation | source freshness, rebuild lag/failure, consumer degraded-mode indicator |
| Replica divergence | per-destination ACK/version, repair status, selected conflict winner |

Logs alone are insufficient for required persistence. Success metrics must
represent acknowledged stages, not merely helper invocation.

## 29. Product Decision Questions

1. **Q1 PG events:** Is loss of `trade_events` acceptable within an explicit
   audit-loss budget, or must each event be durably retried?
2. **Q2 Close ledger:** Does exchange-flat count as completed business close
   when authoritative close accounting has not persisted?
3. **Q3 Redis state:** May the system continue holding/opening positions when
   operational state persistence is unavailable?
4. **Q4 Marker:** Should marker/lease persistence ever block a risk-reducing
   close, or only block ownership/finalization/reopen transitions?
5. **Q5 Notification:** Is state best-effort attempt suppression, at-least-once
   delivery, bounded retry, or escalation?
6. **Q6 Journal:** Is a durable lifecycle operation journal required in Phase 10
   for open/protection/close restart recovery?
7. **Q7 Accounting sink:** Which sink is authoritative for close/partial
   accounting: PG episode, another journal, or reconstructable exchange fills?
8. **Q8 Duplicate versus loss:** For ghost/replay, which is preferable when a
   sink is not perfectly idempotent?
9. **Q9 File durability:** Is the local file merely emergency fallback, or a
   required independent replica with fsync/version guarantees?
10. **Q10 CH gate:** Should missing historical analysis remain fail-open for new
    trades, or is it a required risk input?

## 30. Recommended Policy Table

| Sink/action | Role | Current policy/effect | Recommended candidate | Decision needed |
|---|---|---|---|---|
| Exchange position/order | physical truth | external ACK/queries; ambiguity collapses inconsistently | `UNKNOWN` on ambiguous effect; reconcile before retry | exchange recovery deadlines |
| `pm:positions` lifecycle state | operational/recovery metadata | unacknowledged best effort snapshot | `REQUIRED + RETRYABLE` projection for managed stage; exchange remains physical truth | may opens/holds continue degraded? |
| Local mapped file | secondary recovery copy | best-effort direct overwrite | `BEST_EFFORT` until real durability/version ACK exists | emergency copy or required replica? |
| Closed marker | coordination/dedup/cooldown | unacknowledged Redis-only timestamp | current `BEST_EFFORT` degraded; future typed lease may be `REQUIRED` for exclusive finalization | close/reopen gating scope |
| Notification pending/seen | notification state | seen-before-delivery suppression | `BEST_EFFORT` for attempt model or `REQUIRED + RETRYABLE` for delivery subsystem | PMB-26B2 model |
| PG trade event | audit ledger | usually best effort, mixed exception topology | explicit `BEST_EFFORT` candidate with metrics; optional promotion to required outbox | Q1 / PMB-26C1 |
| PG episode/close accounting | accounting ledger candidate | result discarded, no recovery reader | likely `REQUIRED + RETRYABLE` authoritative finalization record | select authoritative sink and duplicate/loss preference |
| CH trade history | historical analytics/dedup input | best effort inserts; fail-open queries | `BEST_EFFORT`; downstream idempotency needed | accepted loss/retention |
| CH trade analysis | derived advisory input | memory queue; fail-open reads | `DERIVED + BEST_EFFORT`, with degradation metrics | whether gate must fail closed |
| Partial accumulator | accounting staging | Redis-only additive state | `REQUIRED + RETRYABLE` if authoritative slices are not elsewhere | fill reconstruction policy |
| Protection desired state | safety workflow | memory-only task and metadata hints | `REQUIRED + RETRYABLE`; `UNKNOWN` on exchange ambiguity | D2 SLO/failure policy |
| Operation journal | recovery lineage | absent | `REQUIRED` candidate for D3 in-flight operations | Q6/backend/retention |

## 31. Minimal Policy Model

Recommended minimum, pending product approval:

```text
Binance = physical exposure/order truth

required durable operation/episode state
  = proof of incomplete/completed local lifecycle stage

pm:positions
  = required operational projection for managed positions,
    reconstructable only for physical fields

PG trade_events
  = explicit best-effort audit with mandatory loss observability

authoritative close/accounting record
  = product decision; likely required and retryable

ClickHouse
  = best-effort historical + derived advisory data, not lifecycle recovery

notification state
  = separate product delivery policy; never implicit position truth

protection desired state
  = required/retryable by episode and generation

operation journal
  = required candidate for in-flight recovery, not exchange truth
```

This model changes no current behavior. It defines the target semantics D3 must
integrate.

## 32. Full Reliability Model

Must-have foundation:

- D1 durable request identity and UNKNOWN submission state;
- D5 immutable episode/slot/generation identity;
- D2 durable desired protection and verified active state;
- one required operation journal/finalization authority;
- explicit sink acknowledgements and idempotent effect identities;
- restart reconciliation against exchange facts.

Future hardening:

- transactional outbox for required PG/CH/TG fan-out;
- per-entity state revision/CAS and replica repair;
- bounded retry/dead-letter/escalation workers;
- automated exchange order/fill reconstruction;
- health-based open admission and degraded modes;
- audited migration and historical alias repair.

## 33. Relations To D1, D2, And D5

### D1

Request reservation and UNKNOWN stages need durable persistence before and after
submission. Whether Redis, PG, or a journal owns that record is a D3 backend
decision constrained by this D4 acknowledgement policy. An in-memory lock is
not persistence.

### D2

`PROTECTION_PENDING` must survive restart if protection is required. A queue
append is not a durable ACK. D4 classifies desired protection as required and
retryable under the approved D2 SLO, with exchange mutations becoming UNKNOWN
when acknowledgements are ambiguous.

### D5

Episode identity, slot generation, marker token, and protection generation need
a durable owner. D4 requires explicit acknowledgement/version semantics before
those fields can fence stale work. A best-effort whole snapshot cannot be the
only generation authority.

## 34. D3 Input Contract

D4 provides these inputs to P10-D3:

### Required writes

- request/open operation identity before exchange submit;
- submission/UNKNOWN/known exchange references after submit;
- episode activation and required operational projection after fill;
- durable desired protection and generation;
- close operation/progress and partial-fill identities;
- required close/accounting finalization before retiring recovery sources;
- marker/lease generation when used for exclusive transition ownership.

### Best-effort writes, under recommended candidates

- PG `trade_events`, unless product promotes them;
- ClickHouse history/analysis;
- Telegram and notification state under current attempt model;
- local fallback file until a stronger replica contract exists.

### Recovery/replay sources

- Binance positions/orders/fills/Algo orders for physical facts;
- durable operation journal for incomplete-stage lineage;
- canonical episode/generation state for ownership;
- authoritative close/accounting record;
- Redis operational projection as a current view, not sole journal.

### Ordering constraints

- durable intent before irreversible submit;
- UNKNOWN before any blind retry;
- known effect aliases/stage before optional fan-out;
- protection intent before disposable worker handoff;
- finalization intent/record before destructive removal;
- optional sinks after core safety stages.

### Failure policy

- retry idempotent writes by identity;
- reconcile ambiguous commits/effects;
- compensate only through explicit product policy;
- never repeat an exchange business operation merely to repair a ledger.

## 35. Suggested Implementation Split

Future planning only:

| ID | Scope | Readiness |
|---|---|---|
| P10-D4A Operational State ACK | Redis/file primary/replica ACK, degradation, revision/CAS contract | BLOCKED_BY_PRODUCT_DECISION_AND_D3 |
| P10-D4B Event Audit Contract | PG event required vs best effort, loss budget, retry/outbox | BLOCKED_BY_PRODUCT_DECISION |
| P10-D4C Close Accounting Authority | authoritative sink, partial slices, finalization ACK, duplicate/loss | BLOCKED_BY_PRODUCT_DECISION |
| P10-D4D Historical/Notification Delivery | CH loss policy and B2 delivery acknowledgement | BLOCKED_BY_PRODUCT_DECISION |
| P10-D4E Operation Journal | stage schema, backend, ownership, retention, replay | READY_FOR_D3_DESIGN |

No implementation ticket is authorized now.

## 36. Readiness And Conclusion

P10-D4 architecture is **DESIGN AUDITED**.

| Ticket | Readiness | Reason |
|---|---|---|
| PMB-23A | `BLOCKED_BY_PRODUCT_DECISION` | authoritative recorder sink and duplicate-versus-loss preference remain undefined; implementation also needs D3 |
| PMB-26C1 | `BLOCKED_BY_PRODUCT_DECISION` | PG event audit must be approved as best effort or required |
| PMB-26C2 | `BLOCKED_BY_D3` | partial commits need C1/B2 decisions plus integrated staged replay/isolation |
| T12-C | `BLOCKED_BY_D3` | save acknowledgement, revision/CAS, and finalization ordering must be designed in the integrated saga |
| P10-D3 | `READY_FOR_DESIGN` | D1, D2, D4, and D5 architecture inputs now exist; behavior implementation remains product-blocked |

Recommended P10-05 is **P10-D3 Crash Consistency Model**. It should integrate
the request, episode, protection, persistence, acknowledgement, ordering, and
recovery contracts without implementing behavior.

**P10-04 PASS.** Sink inventory, roles, authority, acknowledgement, partial
commits, retry safety, failure domains, journal/outbox options, ordering,
compensation, observability, product decisions, and D3 input contract are
explicit. No persistence helper, sink, exception boundary, retry, transaction,
outbox, journal, lifecycle, reconcile, or production behavior was changed.
