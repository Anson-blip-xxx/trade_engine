# P10-02 / P10-D2 - Protection Establishment Model

> Architecture and semantic design only. Production code is unchanged. This
> document defines when a filled position is protected, how protection work is
> identified, and which failure/restart decisions are required before T5 can
> become an implementation ticket.

## 1. Scope And Guardrails

This model targets the active S6/S8 path in
`strategies.shared_executor.open_position` and the AlgoSL worker in
`shared.position_manager` / `position_runtime.runtime`.

It does not authorize changes to:

- open success or return values;
- queue shape, ordering, bounds, or persistence;
- worker cadence, retries, or lifecycle;
- cancel-before-create ordering;
- Binance request or response interpretation;
- local position/protection persistence;
- close, monitor, ghost, or reconcile behavior.

The legacy `PositionLifecycleService.open_position` also enqueues protection,
but it is not the active S6/S8 open path. Any future implementation must state
whether it covers both paths or retires the legacy one.

## 2. Current Active Path

```text
Binance MARKET response classified as filled
-> _update_pos_cache(..., algo_sl_id=0)
-> best-effort Redis pm:positions write
-> synchronous PG OPEN_ORDER_FILLED call
-> synchronous Telegram call and log
-> _algo_enqueue(symbol, close_side, stop_price, filled_qty)
-> open_position returns True

process-local daemon worker, started as an import side effect:
-> lock queue
-> pop(0)
-> exchangeInfo request and best-effort rounding
-> allAlgoOrders(symbol)
-> DELETE each NEW/WORKING/TRIGGERED order, without confirmed aggregate result
-> POST STOP_MARKET algoOrder
-> any dict containing algoId is classified as success
-> best-effort load/update/save of local algo_sl_id
-> sleep 11 seconds after the attempt
```

The four-field queue item is:

```text
(symbol, side, trigger_price, qty)
```

It has no request ID, position episode ID, generation, enqueue timestamp,
attempt count, deadline, owner, or stale-work fence.

## 3. Protection Vocabulary

The following observations are deliberately distinct:

| Observation | Meaning | Proves exchange protection? |
|---|---|---:|
| Open fill accepted | physical exposure exists | no |
| Local `sl` stored | a desired stop price is available as metadata | no |
| Worker started | a process-local consumer was requested | no |
| Task enqueued | disposable work intent exists in one process | no |
| Task popped | one worker owns an in-memory attempt | no |
| Cancel requests sent | some old orders may have been removed | no |
| Create request sent | outcome may still be ambiguous | no |
| Response contains `algoId` | exchange provisionally acknowledged an identity | not by itself |
| Local `algo_sl_id` stored | a recoverable alias may exist | no |
| Matching active exchange order verified | current generation is exchange-resident | **yes** |

### Normative definition

For this Phase 10 model:

**PROTECTED means that an exchange observation proves an active reduce-only
protection order exists for the current position episode and current protection
generation, with the intended symbol, closing side, trigger, and covered
quantity under the approved quantity policy.**

Verification may be a post-submit exchange query or an ACK whose documented and
tested API contract is itself equivalent to that observation. The repository
currently documents neither such an ACK guarantee nor a post-ACK verification.
Therefore current code can prove only `ACK_ACCEPTED`, not this model's strong
`PROTECTED` state.

`algo_sl_id` is an alias for recovery. It is not proof that the order remains
active, belongs to the current episode, has the current trigger/quantity, or was
not cancelled elsewhere.

## 4. Protection State Machine

This is a semantic state machine, not a selected storage implementation:

| State | Meaning | Exchange-verifiable? | Terminal? |
|---|---|---:|---:|
| `NONE` | no protection is desired for this episode, or no intent exists yet | absence may be queried | no |
| `PENDING` | desired protection is durably recorded but no attempt owns it | no | no |
| `SUBMITTING` | one fenced attempt is performing cancel/query/create work | no | no |
| `UNKNOWN` | an exchange mutation may have succeeded but its outcome is ambiguous | requires query | no |
| `ACTIVE` | matching current-generation protection has been exchange-verified | yes; this is `PROTECTED` | no |
| `REPLACING` | generation N is being superseded by N+1 under an explicit overlap/gap policy | old/new order facts must be queried | no |
| `FAILED` | retry budget/deadline ended and failure policy must run | maybe | policy terminal |
| `CANCELLED` | exchange confirms this generation is no longer active | yes | yes for generation |
| `SUPERSEDED` | a newer desired generation owns protection for the episode | local generation fact plus exchange cleanup | yes for generation |

Candidate transitions:

```text
NONE -> PENDING -> SUBMITTING -> ACTIVE
                         |          |
                         v          v
                      UNKNOWN    REPLACING -> ACTIVE(new generation)
                         |              |
                         v              v
                 ACTIVE / FAILED     UNKNOWN / FAILED

PENDING / SUBMITTING / UNKNOWN / ACTIVE / REPLACING
-> CANCELLED or SUPERSEDED when a fenced close/new-generation decision wins
```

`SUBMITTING -> PENDING` is valid only for a known-safe retry. Ambiguous create or
cancel outcomes go to `UNKNOWN`, not directly to `PENDING` or `FAILED`.

## 5. Identity Chain And Generation

Protection identity must remain separate from open request identity and
position identity:

```text
open_request_id
  -> exchange order/fill aliases
  -> position_episode_id
  -> protection_generation 1..N
  -> exchange algo-order aliases and attempt history
```

- `open_request_id` identifies one logical open intent from P10-D1.
- `position_episode_id` identifies one lifecycle episode in the exchange slot.
- `protection_generation` identifies one desired stop specification for that
  episode.
- `algoId` identifies one exchange attempt/order, not the desired generation.

One generation can have multiple exchange aliases because retry/recovery may
discover an accepted prior attempt. A new trigger or covered-quantity decision
creates generation N+1. A retry of the same desired specification retains N.

A candidate durable key is conceptually:

```text
(account, environment, exchange_position_key, position_episode_id,
 protection_generation)
```

The concrete episode/key schema is deferred to P10-D5. Symbol alone is
insufficient because a stale task for an old episode can run after close and
reopen. Side alone is also insufficient in one-way aggregate mode.

### Generation fencing rule

Before every destructive cancel, create, and local writeback, a worker must
confirm that its episode and generation still own the desired state. A stale
worker must not cancel current protection, create an old stop, or overwrite the
current `algo_sl_id`.

The present four-tuple cannot perform this check.

## 6. Fill-To-Protection Timeline

```text
T0  exchange MARKET fill is accepted/observed
T1  local position state is constructed and best-effort persisted
T2  PG call completes or raises/returns
T3  Telegram/log work completes
T4  protection task is appended to process memory
T5  worker pops the task
T6  exchangeInfo and cancel-old work completes
T7  create request is sent
T8  create response containing algoId is observed
T9  matching active exchange protection is verified (absent today)
T10 local alias/state writeback completes
```

For this model the SLO interval is `T9 - T0`, not enqueue latency (`T4 - T0`),
worker pickup (`T5 - T0`), request latency (`T8 - T7`), or local writeback.

The current path has an **UNBOUNDED FILL-TO-PROTECTION GAP**. It is not a fixed
11-second gap:

```text
current gap >= pre-enqueue PG/TG/log duration
             + idle polling delay (normally 0..1s)
             + queue wait
             + predecessor exchange IO
             + 11s post-attempt pacing per predecessor
             + current exchangeInfo/cancel/create IO
             + verification duration (not implemented)
```

The 11-second sleep happens after each task attempt. It does not intentionally
delay the first idle task, but it delays every queued successor. Network stalls,
queue growth, a stuck predecessor, worker death, and process restart add no
repository-enforced upper bound.

## 7. Required SLO Contract

T5 needs numeric product values before implementation. The contract must name:

| Dimension | Required decision |
|---|---|
| Start observation | first confirmed fill, final accepted fill, or another explicit exchange event |
| Success observation | this document recommends verified `ACTIVE` at T9 |
| Latency target | percentile objective and hard maximum from start to success |
| Coverage | initial stop, replacement stop, partial-fill adjustment, and restart recovery |
| Quantity tolerance | exact full exposure, at least exposure, or another bounded rule |
| UNKNOWN grace | maximum query/reconciliation interval before escalation |
| Retry budget | attempts, elapsed deadline, backoff, and rate-limit budget |
| Availability budget | acceptable rate/duration of unprotected exposure |
| Failure action deadline | when hold, close, quarantine, or operator escalation must begin |
| Measurement | durable timestamps, metrics, alert thresholds, and owner |

Without these values, neither queue capacity nor worker concurrency can be
derived and no implementation can demonstrate compliance.

## 8. Queue, Worker, And Backpressure Facts

Current behavior:

- `_ALGO_QUEUE` is an unbounded process-local list;
- enqueue always appends under a local lock and has no rejection/backpressure;
- FIFO uses `pop(0)` and has no priority for initial protection over trailing
  replacement;
- there is one daemon worker per importing process, gated by a boolean flag;
- the flag means start was attempted, not that the thread remains alive;
- a worker exception is logged and consumed work is not requeued;
- there is no heartbeat, queue age/depth metric, last-success timestamp, failure
  counter, dead-letter state, or admission health gate;
- S6 and S8 processes have independent queues and workers;
- restart clears queue contents, in-flight ownership, and the started flag.

An unbounded queue cannot report `FULL`; it converts overload into growing
unprotected latency and memory use. Backpressure policy therefore requires an
explicit bounded capacity/deadline and a response when capacity is unavailable.

Open admission currently does not consult worker liveness, queue age/depth,
recent protection success, or exchange AlgoSL availability.

## 9. Protection Source-Of-Truth Matrix

| Fact | Current source | Authority in this model | Current recovery limitation |
|---|---|---|---|
| Physical exposure | Binance position state | exchange | available through polling/reconcile |
| Desired stop price | Redis/local position `sl` | future durable desired-protection record | current field has no operation status/generation |
| Pending work | process `_ALGO_QUEUE` | future durable work/desired state | lost on process death |
| Active protection | Binance Algo orders | exchange | queried for cancellation, not adopted/verified after create |
| Exchange protection alias | response/local `algo_sl_id` | secondary lookup alias | best-effort write; can be absent or stale |
| Current generation | absent | future durable desired state | cannot fence stale work today |
| Cancel completion | raw DELETE effects | exchange-confirmed observation | aggregate success is not checked |
| Replacement completion | absent | verified ACTIVE generation N+1 | no current state transition |

Exchange is authoritative for observed active protection. Durable local desired
state must become authoritative for what should exist. Reconciliation compares
the two; neither `sl` nor `algo_sl_id` alone wins that comparison.

## 10. Current Failure Matrix

| ID | Failure | Current result | Recovery today | Required future classification |
|---|---|---|---|---|
| F1 | Queue overload/full | queue has no bound or full signal; latency/memory grow | none | admission/backpressure or deadline failure |
| F2 | Enqueue raises | active open logs and still returns `True` | none | durable `PENDING` or immediate failure policy |
| F3 | Process dies before enqueue | fill/state may exist, intent absent | exchange/local polling does not rebuild it | reconstruct desired generation from durable open state |
| F4 | Process dies after enqueue/before pop | tuple is lost | restart creates empty queue | replay durable pending work |
| F5 | Worker dead/stuck | flag can remain true; backlog grows | no liveness restart or alert | health state, lease/heartbeat, ownership recovery |
| F6 | exchangeInfo fails | rounding is skipped and placement continues | one attempt only | classify validation risk separately from create outcome |
| F7 | cancel-old fails/unknown | error is swallowed; create still proceeds | later cancel-all may retry incidentally | query and resolve possible old active order |
| F8 | create rejects/returns malformed | one log, task consumed, no alias update | no retry/requeue | `FAILED` or retryable `PENDING` by error class |
| F9 | create ACK is ambiguous | transport exception becomes error result; accepted order may exist | no post-failure query | `UNKNOWN`, identity-directed exchange recovery |
| F10 | ACK succeeds but local save fails | exchange order may be active; alias absent | later worker may cancel it; no adoption scan | retain ACTIVE from exchange truth and repair alias |

Other important failures:

- PG/TG delay before enqueue extends naked exposure;
- full close cancels exchange orders but does not invalidate queued tasks;
- local stop mutation can commit before replacement is active;
- a worker can write an old `algoId` into metadata for a newer episode;
- two processes can independently cancel/create protection for the same symbol.

## 11. Ambiguous Create And Exchange Verification

Current success logic is exactly:

```text
isinstance(result, dict) and 'algoId' in result
```

It does not inspect a documented accepted/working status and performs no
post-create query. Conversely, a timeout or exception does not prove rejection.

Future UNKNOWN handling should be identity-directed:

1. Persist `UNKNOWN` for the same episode/generation/attempt.
2. Do not issue a blind duplicate create.
3. Query directly by a characterized deterministic exchange identity if the
   API supports it.
4. Otherwise inspect a bounded symbol Algo-order set and match side, type,
   trigger, quantity, timestamps, and generation aliases.
5. Confirm current exposure/episode before adopting or cancelling an order.
6. Transition to ACTIVE, safe retry, FAILED, or operator quarantine only under
   the approved ambiguity policy.

The repository's `allAlgoOrders(symbol)` query is currently used only before
placement to find statuses `NEW`, `WORKING`, and `TRIGGERED` for cancellation.
`openAlgoOrders` is used by monitoring metrics. There is no production query
that verifies the just-created `algoId` as matching active protection.

## 12. Retry Model Comparison

| Model | Handles transient reject | Handles ambiguous ACK | Restart-safe | Duplicate risk | SLO behavior |
|---|---:|---:|---:|---:|---|
| No retry (current) | no | no | no | low create retry risk, high loss risk | unbounded failure |
| Immediate blind retry | sometimes | **no** | no | high | fast but unsafe |
| In-memory bounded retry | yes while alive | only with query | no | medium | process-dependent |
| Durable retry with backoff | yes | only with query/identity | yes | bounded by fencing | measurable |
| Desired-state reconciler | yes | yes if exchange matching is sound | yes | lowest with generation fencing | convergent, needs deadlines |

Backoff controls load; it does not provide idempotency. Retry correctness
requires stable generation identity, exchange observation, and stale-owner
fencing.

## 13. Cancel-Then-Create And Replacement Outcomes

Current placement calls `_cancel_all_algo(symbol)` and then creates. Stop
updates can also cancel by old ID or cancel-all before enqueue, followed by a
second cancel-all in the worker.

| Cancel-old observation | Create-new observation | Possible active protections | Current classification |
|---|---|---:|---|
| confirmed success | confirmed active | 1 | treated as success only from create ACK |
| confirmed success | confirmed reject | 0 | log only |
| confirmed success | unknown | 0 or 1 | consumed task |
| failed/unknown | confirmed active | 1 or 2 | duplicate protection possible |
| failed/unknown | confirmed reject | 0 or 1 | old protection unknown |
| failed/unknown | unknown | 0, 1, or 2 | no convergence proof |

More than two can accumulate across multiple workers/attempts. PMB-9's DELETE
transport correction does not make cancel-then-create atomic and does not add
aggregate cancel acknowledgement, generation identity, or verification.

Replacement policy must explicitly choose one of these safety trade-offs:

- cancel-first: permits a zero-protection interval;
- create-first: permits overlapping protection and requires exchange/product
  support for multiple reduce-only orders;
- modify-in-place: only if the API contract is characterized and atomic enough;
- close-on-inability-to-replace: limits exposure but is a product compensation.

No choice is selected here.

## 14. Stale Work And Race Matrix

| Race | Current possible outcome | Required future fence/reconcile result |
|---|---|---|
| Position closes after enqueue, before pop | stale create runs after close | close generation/tombstone invalidates task before mutation |
| Same symbol reopens before stale pop | old task can cancel new protection and place old stop | episode ID mismatch makes old task `SUPERSEDED` |
| Generation N+1 queues behind N | old stop may become active first for an arbitrary interval | latest desired generation and deadline are explicit |
| Two processes replace same symbol | interleaved cancel/create can leave 0, 1, 2+ orders | one fenced owner or convergent generation reconciliation |
| Close and worker create overlap | close cancel can precede later stale create | close fence wins and post-create check removes stale result |
| BE/trailing update and worker overlap | local `sl` may describe N+1 while exchange has N or none | generation state distinguishes desired from active |
| Partial close changes quantity | queued task can retain old quantity | quantity policy creates/reconciles a new generation |
| Worker ACK writeback after reopen | old `algoId` can overwrite current symbol metadata | compare episode/generation before writeback |
| Ghost/reconcile removes/adopts state | no pending intent is rebuilt or invalidated | lifecycle reconciliation also reconciles desired protection |

FIFO ordering inside one process is not a stale-work fence. Cross-process order
is unconstrained, and restart removes FIFO history.

## 15. Close And Restart Semantics

Full real close calls exchange cancel-all after flatness is confirmed. It does
not inspect or purge `_ALGO_QUEUE`. Sandbox close does not cancel exchange Algo
orders. Partial close does not update protection quantity. Ghost cleanup and
silent reconcile do not rebuild or invalidate pending protection.

A future close contract must:

1. durably advance/invalidate the episode's protection generation;
2. prevent pending/submitting stale workers from committing;
3. reconcile exchange orders after ambiguous cancel;
4. allow restart to resume cleanup without attaching work to a later episode.

Restart recovery must not infer desired work from `algo_sl_id == 0` alone. Zero
can mean never requested, pending lost, create failed, create succeeded but save
failed, or intentionally no protection. Recovery needs a durable state and
generation plus exchange facts.

Minimum startup reconciliation inputs are:

```text
durable current position episode
+ durable desired protection generation/state
+ bounded exchange position snapshot
+ bounded exchange active Algo-order snapshot
```

Current startup/reconcile paths provide no such workflow replay.

## 16. Worker Health And Open Admission

A future worker health signal should distinguish:

```text
NOT_STARTED
RUNNING_HEALTHY
RUNNING_DEGRADED
STALLED
DEAD
EXCHANGE_UNAVAILABLE
```

At minimum it needs queue depth, oldest age/deadline, in-flight age, last
attempt, last verified success, consecutive failures, and owner/lease identity.
The current `_ALGO_WORKER_STARTED` boolean proves none of these.

Product must decide open admission under unhealthy protection:

| Policy | Behavior | Trade-off |
|---|---|---|
| Fail closed | reject new opens before MARKET order | strongest prevention, lower availability |
| Bounded degrade | admit only if synchronous/alternate protection can meet deadline | complex but safety-oriented |
| Admit then compensate | fill, attempt protection, close by deadline on failure | market/slippage cost and partial-commit complexity |
| Fail open | continue opens and alert | highest unprotected exposure |

The current behavior is effectively fail open after fill: enqueue/create
failures do not change the active open's `True` result.

## 17. Durable State Options

| Option | Pending/restart | Atomic relation to position state | Distributed ownership | Main limitation |
|---|---:|---:|---:|---|
| Process queue (current) | no | none | no | disposable and unbounded |
| Redis durable desired record | yes if Redis persistence is approved | possible CAS with Redis position metadata | shared only under deployment assumptions | exchange remains outside transaction |
| PG operation/outbox row | yes | can relate to PG open operation if PG is required | strong claims/leases possible | PG is currently optional/write-only |
| Durable message queue | yes | needs outbox/idempotent producer | consumer group/lease dependent | desired-state authority still needed |
| Desired-state reconciler plus durable store | yes | explicit episode/generation state | convergent with fencing | largest semantic change |

No backend is selected. Redis persistence/topology and PG authority remain open
Phase 10 decisions. A durable queue without durable desired state still cannot
decide whether replayed work is current.

## 18. Emergency Failure Policies

When protection cannot be verified by the SLO deadline, candidate policies are:

| Policy | Exposure result | Required support |
|---|---|---|
| Hold and retry | position remains intentionally unprotected | explicit risk approval, alerts, bounded retry |
| Immediate reduce-only close | attempts to remove exposure | close idempotency and ambiguous-close recovery |
| Quarantine | block new actions and escalate operator | durable state and operational response |
| Alternate protection path | use another exchange-native order/path | API characterization and duplicate reconciliation |
| Account/system open halt | prevents additional exposure | health aggregation and admission gate |

These can be combined, such as immediate close plus account open halt. The
choice is product policy and cannot be inferred from existing logs.

## 19. Current, Minimal, And Full Models

### Current model

```text
best-effort local sl
+ disposable FIFO tuple
+ one unmonitored daemon consumer per process
+ cancel-all then one create attempt
+ algoId-presence acknowledgement
+ best-effort alias writeback
```

It provides no bounded establishment guarantee.

### Minimal Phase 10 candidate

```text
stable position episode ID from P10-D5
+ monotonically increasing protection generation
+ durable desired state written immediately after a confirmed fill
+ prompt first attempt with bounded deadline/backpressure
+ UNKNOWN state and bounded exchange verification
+ restart replay
+ close/reopen generation fencing
+ explicit SLO failure action
```

This is a semantic minimum, not a backend selection. It may retain asynchronous
placement if the approved SLO can be demonstrated.

### Full reliability model

```text
durable open-operation journal from D1/D3
+ canonical request/order/fill/episode aliases from D5
+ desired protection reconciler
+ fenced distributed ownership
+ exchange client identity and query contract
+ generation-aware replace/partial-close/close workflow
+ health-based admission and emergency compensation
+ metrics, alerts, audit history, and operator controls
```

The full model belongs to the integrated P10-D3 crash/restart design and should
not be implemented as a queue-only T5 patch.

## 20. Product And Architecture Decisions

1. **Q1 Protection SLO:** What percentile and hard maximum are allowed from
   confirmed fill to verified ACTIVE protection?
2. **Q2 Open success:** Does open return success before protection, only after
   protection, or as a staged `FILLED_UNPROTECTED` result?
3. **Q3 Deadline action:** On failed/unknown protection, hold, close, quarantine,
   halt opens, or combine these actions?
4. **Q4 Admission:** Must an unhealthy/stalled protection pipeline reject new
   opens before exchange submission?
5. **Q5 Replacement:** Is any zero-protection interval allowed, and may old/new
   reduce-only protections overlap?
6. **Q6 Quantity:** Must active stop quantity exactly match current exposure,
   and how do partial fills/closes advance generation?
7. **Q7 Restart:** Is replay mandatory after process, host, and worker failover?
8. **Q8 Deployment:** How many S6/S8 replicas/hosts may mutate one account and
   which durable coordinator is shared?
9. **Q9 UNKNOWN:** How long may ambiguity remain before compensation/operator
   action, and which exchange query is authoritative?
10. **Q10 API contract:** Which Algo statuses are active/protective, and does an
    ACK containing `algoId` guarantee immediately queryable active protection?

Q1, Q3, Q4, Q5, and Q7 are direct product blockers for T5. Q6, Q8, Q9, and Q10
also require architecture/API characterization.

## 21. Relations To Other Phase 10 Tickets

### P10-D1 / T1-B

The open request ID can link the protection workflow to the originating intent,
but it cannot replace position episode or protection generation identity.
Protection recovery must never resubmit the MARKET open merely because
protection is pending.

### P10-D5 / POS-ID

P10-D5 must provide a stable episode key and exchange-position scope before
generation fencing can be implemented safely. D2 defines the relation but does
not choose the canonical schema.

### P10-D3 / T12-A And T12-D

D3 integrates `FILLED -> PROTECTION_PENDING -> PROTECTED/FAILED` with open crash
recovery and restart replay. D2 supplies the protection states, truth rules,
UNKNOWN behavior, and deadline inputs.

### P10-D4

D4 decides whether PG/outbox persistence can be required in the protection
commit path. Current PG calls are not a protection queue or recovery journal.

## 22. Suggested Implementation Ticket Split

These tickets are future planning only:

| ID | Scope | Current readiness |
|---|---|---|
| P10-D2A Desired Protection Record | durable episode/generation/spec/status schema and atomic transition contract | BLOCKED_BY_D5 |
| P10-D2B Durable Handoff And Replay | fill-to-pending handoff, ownership, restart scan, backpressure | BLOCKED_BY_D2A |
| P10-D2C Exchange Verification Contract | Algo ACK/status/query semantics, identity matching, sandbox characterization | NEEDS_CHARACTERIZATION |
| P10-D2D Generation-Fenced Reconcile | stale suppression, UNKNOWN recovery, cancel/replace convergence | BLOCKED_BY_D2A_D2C |
| P10-D2E SLO Admission And Emergency Policy | health metrics, admission gate, deadline action, alerts | BLOCKED_BY_PRODUCT_DECISION |

No split is authorized for production implementation yet.

## 23. Implementation Guardrails

- Do not describe enqueue, worker pickup, request send, `algoId` presence, or
  local writeback as `PROTECTED` without the approved exchange contract.
- Do not treat the post-attempt 11-second sleep as the total exposure gap.
- Do not add blind retries for UNKNOWN create/cancel outcomes.
- Do not replay a task using symbol alone.
- Do not use `algo_sl_id` as episode identity or active-order truth.
- Do not solve durability with a bounded in-memory queue.
- Do not add a durable queue without generation-aware desired state.
- Do not cancel current protection from an unfenced stale worker.
- Do not silently choose cancel-first versus create-first replacement policy.
- Do not infer worker health from `_ALGO_WORKER_STARTED`.
- Preserve current production behavior until product/API decisions are approved.

## 24. Readiness And Conclusion

P10-D2 architecture is **DESIGN AUDITED**.

T5 implementation readiness is:

**BLOCKED_BY_PRODUCT_DECISION**

The primary blockers are the numeric fill-to-verified-protection SLO, open
success/admission semantics, deadline failure action, replacement gap/overlap
policy, and mandatory restart guarantee. Implementation also depends on P10-D5
for stable position episode identity and on exchange API characterization for
ACK/query/status semantics.

**P10-02 PASS.** The current gap is correctly classified as unbounded; the
strong PROTECTED state, generation model, failure/restart contract, and future
ticket split are explicit. No queue, worker, sleep, retry, cancel, placement,
persistence, lifecycle, or other production behavior was changed.
