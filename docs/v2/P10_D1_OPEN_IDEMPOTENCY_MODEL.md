# P10-01 / P10-D1 - Open Idempotency Model

> Architecture and semantic design only. Production code is unchanged. This
> document defines what one open intent means before selecting a lock,
> reservation backend, client order ID, or recovery implementation.

## 1. Active Open Path

The active S6/S8 path is not the legacy PositionLifecycleService open path:

```text
S3 or TradingView event snapshot
-> shared_executor.read_all_signals
-> S6._open_long or S8._open_short
-> strategy gates + local/exchange position checks
-> strategies.shared_executor.open_position
-> execution.core.se_open_intent
-> ExecutionService.execute_order
-> SharedExecutorBinanceAdapter.place_order
-> fapi_post('/fapi/v1/order')
-> Binance Futures
```

The only in-repository production callers of the active function are S6 and S8.
The legacy `shared.position_manager.open_position` delegates to
`PositionLifecycleService.open_position`, has no in-repository production
caller, and received the Phase 9 T1-A local precheck fix.

### Active execution order

```text
outer has_any_position(symbol)
-> strategy/risk calculations
-> inner positionRisk(symbol)
-> leverage request
-> margin request
-> one MARKET order submission
-> parse and classify exchange response
-> generate position_id and publish local state
-> PG OPEN_ORDER_FILLED event
-> Telegram/log
-> enqueue in-memory AlgoSL task
-> return True
```

The outer and inner checks are both check-then-act reads. Neither reserves an
intent or serializes another worker.

## 2. Signal Producers And Open Input Schema

### Producer facts

S3 produces event snapshots containing `type`, `symbol`, `strength`, event
details, lifecycle `state`, and `since`. The outer snapshot has `ts`, which the
reader exposes as `_snapshot_ts`. The original candle `t` is dropped before
event construction, so there is no bar timestamp at the open boundary.

TradingView normalization produces `type`, `symbol`, `strength`, `ts`,
`source='tv'`, `tv_signal`, and `side`, plus optional market/comment fields.
It does not preserve arbitrary event, request, trace, or bar identifiers.

Neither active producer creates a stable `event_id`. Unified Signal and Journal
models can hold event/correlation IDs, but the active producers do not supply
them and those observer objects are not passed into open execution.

### Strategy to shared-executor schema

| Field | S6 | S8 | Exists before order? | Reaches Binance entry order? |
|---|---|---|---:|---:|
| `name/system` | S6A or S6B | S8 | yes | no |
| `symbol` | event symbol | event symbol | yes | yes |
| `side` | LONG | SHORT | yes | converted to BUY/SELL |
| `entry_price` | fresh ticker | fresh ticker | yes | no for MARKET; parser fallback only |
| `stop_price` | derived below price | derived above price | yes | no |
| `qty` | risk-derived | risk-derived | yes | yes after later adjustments/rounding |
| `margin_mode` | event mapping | event mapping | yes | separate margin request |
| `leverage` | score/ATR derived | score/ATR derived | yes | separate leverage request |
| `event_type` | original type | original type | yes | no |
| `strength` | final contract score | final contract score | yes | no |
| `expected_move_pct` | event-derived | event-derived | yes | no; R:R gate only |
| `decision_context` | fixed telemetry subset | fixed telemetry subset | yes | no; PG payload after fill |
| producer `source` | implicit S3 or TV | implicit S3 or TV | event-only | no; dropped |
| producer timestamp | `since`/snapshot | `since`/snapshot | event-only | no; dropped |
| bar timestamp | absent | absent | no | no |
| producer `event_id` | absent | absent | no | no |
| `request_id` | absent | absent | no | no |
| trace/correlation ID | observer schema only | observer schema only | no active value | no |

### Actual Binance MARKET payload

```text
symbol
side = BUY or SELL
type = MARKET
quantity
newOrderRespType = RESULT
```

The signed HTTP wrapper adds an authentication timestamp and signature. That
timestamp is not signal identity, request identity, or a replay key.

## 3. Identity Candidate Inventory

| Candidate | Meaning if used | Current availability |
|---|---|---|
| A. symbol | one exchange aggregate slot | available before order |
| B. symbol + side | one directional intent | available before order |
| C. symbol + side + system | one strategy-owned directional intent | available before order |
| D. signal/event ID | one producer event | model supports it, active producers do not create it |
| E. event ID + system | one producer event consumed by one strategy | unavailable because active event ID is absent |
| F. generated request UUID | one consumer-created operation | not generated or propagated today |
| G. Binance client order ID | one broker-visible request | not supported by OrderIntent or sent today |
| H. position ID | one local position episode/accounting identity | generated after accepted fill |
| I. time-bucket key | heuristic grouping by fields and time | can be computed, but no authoritative bucket exists |

## 4. Identity Evaluation Matrix

| Identity | Exists pre-order? | Stable on retry? | Restart-safe? | Distributed-safe? | Supports scale-in? |
|---|---:|---:|---:|---:|---|
| symbol | yes | yes | yes | yes | no if used as unique request key |
| symbol + side | yes | yes | yes | yes | no if unique within active position |
| symbol + side + system | yes | yes | yes | yes | no unless another intent dimension exists |
| producer event ID | no on active producers | potentially if producer guarantees it | potentially | potentially | yes, distinct events can add |
| event ID + system | no today | potentially | potentially | potentially | yes |
| random request UUID generated per call | yes after generation | **no** unless caller persists/reuses it | no by default | value can be shared | yes, but retry may accidentally create a new UUID |
| durable request UUID/key | future only | yes by definition | yes if durably stored | yes if shared | yes, one key per intentional add |
| Binance client order ID | future only | potentially, contract unverified | exchange-side if accepted | potentially | yes, one value per intentional add |
| current position ID | no | no for submission retry | only after state save | value can be shared | represents episode, not request |
| time-bucket key | computable | only inside chosen bucket | recomputable | recomputable | may collapse valid events in same bucket |

No existing candidate simultaneously identifies the current request before
order submission and remains stable across retry, restart, and workers.

## 5. Same-Request Definition

Request identity answers only: "Is this execution attempt for the same logical
open intent?" It does not answer whether another intent may target the same
position slot.

| Scenario | Semantic classification | Current detectability |
|---|---|---|
| Same HTTP/consumer delivery retried with the same durable key | SAME REQUEST | no open API/key exists |
| Same producer signal delivered twice | SAME REQUEST if producer identity is stable | active producers provide no event ID |
| Same symbol/side from a later independent signal | NEW INTENT | distinguishable only by event context, not durable identity |
| Explicit user/strategy scale-in command | NEW INTENT | no explicit scale-in request model exists |
| Network timeout retry of the same submission | SAME REQUEST | indistinguishable today |
| Process restart replay of an incomplete operation | SAME REQUEST | no operation record/replay exists |
| Payload happens to match in a time bucket | NEEDS PRODUCT DECISION | heuristic only; cannot prove sameness |

A request key must be created or inherited once and reused by every retry. A new
random UUID generated on each retry does not provide idempotency.

## 6. Duplicate Scope Matrix

| Scenario | Current behavior | Desired semantic question |
|---|---|---|
| Same event, same system | process-local event cooldown may suppress sequentially; no durable key | Must every delivery converge to one open operation? |
| Same event, different process | both can pass stale checks and submit | How is producer identity shared and claimed? |
| Same symbol+side, new event | normally blocked once position becomes visible | Is this prohibited or an intentional scale-in candidate? |
| Same symbol, opposite side | side-blind gates normally reject once visible | Is one-way symbol exclusivity a permanent product rule? |
| Same symbol, different system | one symbol state slot normally rejects second owner | Can systems co-own or transfer an aggregate position? |
| Manual retry after explicit failure | no request record; may submit again | Is it the same operation or operator-authorized new intent? |
| Timeout retry | `None`/failure can lead to a later second POST | Must remain UNKNOWN until exchange reconciliation? |
| Restart replay | no workflow replay; repeated signal may look new | Must exactly-once intent survive restart? |
| Intentional scale-in | no explicit support; races can accidentally aggregate | What command/identity distinguishes it from duplicate delivery? |

## 7. Current Scale-In Semantics

**CURRENT MODEL = SINGLE ACTIVE SYMBOL POSITION.**

Evidence:

- `has_any_position(symbol)` is side-blind;
- active state is keyed by symbol;
- exchange mode is treated as one-way aggregate exposure;
- local metadata has one system/side/position ID per symbol;
- sequential same-symbol requests are rejected once either local or exchange
  exposure is visible.

Concurrent races can accidentally add, offset, or flip exposure, but accidental
aggregation is not scale-in support. Future product policy may choose scale-in;
the current model must not silently become that future decision.

## 8. Request Idempotency Versus Position Exclusivity

These are separate invariants:

```text
Request idempotency:
  the same request_key must not create two exchange orders.

Position exclusivity:
  decide whether two different request_keys may target the same exchange slot.
```

A request-key uniqueness constraint solves duplicate delivery but permits two
different intents. A symbol lock serializes intents but does not identify a
retry. One mechanism must not silently define both policies.

## 9. Lock Limitation

Golden architecture statement:

**LOCK != RETRY IDEMPOTENCY.**

A per-symbol distributed lock can prevent two cooperating workers from entering
the exchange submission window concurrently. It cannot prevent:

```text
worker A acquires lock
-> exchange accepts order
-> A crashes before durable completion
-> lock TTL expires
-> retry/worker B acquires lock
-> B submits a second order
```

The lock is coordination. Durable request identity and exchange reconciliation
are needed to determine whether a new submission is safe after lock release.

## 10. Request Reservation State Machine

This is a candidate semantic state machine, not an implementation:

| State | Meaning | Locally knowable? | Exchange knowable? | Restart recoverable today? |
|---|---|---:|---:|---:|
| NEW | validated intent, no reservation | yes in caller memory | no | no |
| RESERVED | one owner claimed request/exclusivity scope | future durable state | no | no current record |
| SUBMITTED | POST began, acknowledgement pending | future state | maybe | no |
| UNKNOWN | submission outcome ambiguous | future state | requires query/reconcile | no |
| ACKED | exchange returned an order identity/status | yes from response | yes | only if persisted |
| FILLED | exchange reports accepted fill/exposure | yes from response/query | yes | exposure recoverable, request identity is not |
| STATE_SAVED | local position metadata acknowledged | caller can know only with future save result | no | Redis may survive, operation stage absent |
| PROTECTED | intended exchange protection acknowledged | worker can observe response | yes | exchange order may survive; stage mapping absent |
| FAILED | terminal, safe-to-retry or rejected outcome defined | future state | maybe | no current operation record |

Allowed transition policy, retries, expiry, and terminal semantics require
product/design approval. In particular, `UNKNOWN` must not automatically return
to NEW and resubmit.

## 11. Minimal Future Idempotency State

At minimum, a future durable operation needs:

```text
request_key
symbol
side
system
status
exchange_order_id (nullable)
client_order_id (nullable)
created_at
updated_at
last_error / unknown reason (nullable)
```

Useful but not required in the minimum record are owner/lease metadata,
attempt count, source event reference, desired quantity, and version. The
minimum state should not duplicate an entire position or decision journal.

## 12. Reservation Backend Comparison

| Backend | Concurrency | Restart | Distributed | UNKNOWN acknowledgement | Cleanup | Operational complexity |
|---|---|---|---|---|---|---|
| A. in-memory lock | one process only | lost | no | none | automatic on process death | low |
| B. Redis SETNX + TTL | serializes one key while lease lives | lease survives process briefly | same shared Redis | no durable stage by itself | TTL; abandoned-owner semantics | medium |
| C. Redis hash/state machine | atomic claim plus operation stages possible | yes if Redis persists | same shared Redis | can retain UNKNOWN | explicit TTL/archive/CAS | medium/high |
| D. PostgreSQL unique row | unique durable operation and transitions | yes | yes | can retain/query UNKNOWN | retention/archive policy | high; PG availability enters open path |
| E. exchange client ID only | broker-side duplicate protection if contract supports it | exchange-side | yes | potentially queryable, unverified | exchange retention rules unknown | medium, API-contract dependent |
| F. Redis + client ID hybrid | local coordination plus broker identity | yes with durable Redis state | yes under deployment assumptions | strongest minimal candidate | two-system reconciliation | high |

No backend is selected. Redis is currently localhost and deployment topology is
not proven. PostgreSQL is currently optional. Those facts must be resolved
before either is declared authoritative.

## 13. Redis Lock/Reservation Scope

A future key shaped like `pm:open:{...}` must state which invariant it serves:

| Scope | Coordinates | Hidden policy risk |
|---|---|---|
| symbol | all opens for one exchange slot | silently prohibits scale-in and cross-system concurrency |
| symbol+side | directional opens | unsafe for one-way opposite-side netting |
| symbol+system | one strategy owner | permits conflicting systems on one exchange slot |
| request key | duplicate attempts for one intent | does not enforce position exclusivity |

TTL must exceed the protected critical section or use renewal/fencing. Even a
correct TTL cannot decide whether an expired owner already submitted an order.
Therefore request reservation and position-slot coordination should be separate
records or explicitly separate constraints.

## 14. ClientOrderId Repository Audit

Current state:

- `OrderIntent` has no `newClientOrderId`, `clientOrderId`, or request field;
- the execution port exposes only `place_order`;
- the adapter forwards `intent.to_params()` unchanged;
- no active open sends a client order ID;
- returned `orderId` is available only after acknowledgement;
- there is no ordinary-order query-by-order-ID/client-ID implementation;
- generic signed wrappers could transport arbitrary paths/params, but that is
  not a domain contract or tested recovery primitive.

Repository-visible query primitives are:

- `positionRisk(symbol)` for physical exposure;
- `openOrders(symbol)` in S7;
- `userTrades(symbol, ...)` in S7 and the offline reconciliation script.

The repository does not document client-ID length, charset, uniqueness scope or
window, reuse behavior, query endpoint, query retention, or duplicate/not-found
response semantics. P10-D1C must characterize those constraints before any
client-ID implementation is authorized. No external API facts are assumed here.

## 15. Ambiguous ACK Model

Core scenario:

```text
POST MARKET order
-> Binance may accept/fill it
-> network timeout/non-200/transport exception collapses to None
-> caller returns False without orderId, state, PG event, or SL task
```

Future semantic response:

1. Persist/retain the operation as `UNKNOWN`.
2. Do not immediately POST the same request again.
3. Prefer direct query by deterministic client order ID if repository/API
   characterization proves support.
4. Otherwise use a bounded recovery set: `openOrders`, recent `userTrades`, and
   `positionRisk`.
5. Treat position delta as exposure evidence, not proof of request identity.
6. If still unresolved, keep UNKNOWN for operator/bounded reconciliation rather
   than guessing NEW or FAILED.

## 16. Exchange Query Limits And Minimal Recovery Query

Current wrappers use immediate calls and ten-second timeouts. They do not expose
general rate-limit headers, endpoint weights, jitter, or backoff. Visible query
bounds include S7 `userTrades limit=50` and reconciliation `limit=1000` over
bounded time windows. There is no tested single-order recovery primitive.

The minimum future recovery query should be identity-directed, not "query every
order." Candidate order:

```text
1. direct client-order lookup, only if verified and available
2. symbol open-orders query for accepted but unfilled state
3. bounded recent user-trades query for fills/orderId
4. positionRisk for safety/exposure confirmation
```

Without a stable client ID, steps 2-4 remain heuristic under concurrent similar
orders.

## 17. Request-Identity Crash Matrix

| Point | Request record | Exchange state | Local position state | Safe retry? | Required recovery action |
|---|---|---|---|---|---|
| C1 RESERVED before order, crash | RESERVED/owner expired | no order expected | absent | not until owner/lease and no-submit evidence resolved | reclaim reservation or mark failed under fencing policy |
| C2 order sent before ACK, crash | SUBMITTED/UNKNOWN | absent, open, or filled | absent | **no immediate retry** | query by client ID, then bounded order/fill/exposure reconciliation |
| C3 ACK/FILL before local save, crash | ACKED/FILLED if persisted; otherwise UNKNOWN | order/fill exists | absent | no new POST | reconstruct operation and save metadata/protection intent |
| C4 local save before protection | STATE_SAVED | position exists; protection absent/unknown | present | open retry must return prior operation, not POST | hand off to T5 protection recovery using same operation identity |

Current code has none of the request records shown in this table.

## 18. Distributed Race Matrix

| Race | Required idempotency result | Separate position-policy result |
|---|---|---|
| A/B carry same request key | one owner submits; loser observes/resumes same operation | no second position decision needed |
| A/B carry different keys, same symbol/side/system | both requests are individually valid identities | product decides reject, queue, merge/scale-in, or transfer |
| A/B carry different keys, same symbol/opposite side | not duplicate requests | one-way netting/exclusivity policy decides conflict |
| A/B carry different keys, same symbol/different system | not duplicate requests | ownership policy decides conflict/co-ownership |
| owner crashes while reservation active | another worker must recover existing operation | must not create a new intent merely because lease expired |

The current implementation cannot distinguish same-key and different-key races
because no request key reaches the open boundary.

## 19. Product Decision Questions

1. **Q1 Scale-in:** May a new signal for the same symbol and side intentionally
   increase exposure? If yes, what explicit command/key distinguishes it from a
   duplicate delivery?
2. **Q2 Cross-system ownership:** Do S6A, S6B, and S8 share one active symbol
   slot, or may systems own independent logical portions of one exchange
   aggregate?
3. **Q3 UNKNOWN policy:** After timeout, is delayed reconciliation acceptable,
   or must the system compensate/close rather than wait? Immediate blind retry
   is not safe.
4. **Q4 Idempotency window:** Must request identity remain unique only during
   submission, throughout the active position, or for a historical retention
   period?
5. **Q5 Restart guarantee:** Must exactly-once intent survive process restart,
   host restart, and failover to another worker?
6. **Q6 Duplicate response:** Should a detected same request return prior
   success/result, a pending/unknown result, or explicit duplicate rejection?
7. **Q7 Manual override:** How does an operator intentionally create a new
   request after an UNKNOWN operation without reusing the old identity?

Until Q1-Q5 have approved answers, T1-B cannot be implementation-ready.

## 20. Recommended Layered Model

No concrete backend is selected, but the semantic layers should be separate:

1. **Stable request identity:** inherit a durable producer ID or create one once
   at intent admission and reuse it for every retry/restart.
2. **Durable reservation/state:** atomically claim the request and retain its
   operation stage, including UNKNOWN.
3. **Exchange request identity:** derive/send a broker client ID only after its
   API contract is characterized; persist exchange aliases.
4. **Position exclusivity policy:** independently decide whether different
   request keys can target one symbol slot.
5. **Recovery/reconcile:** resume the operation by identity and exchange facts;
   never infer "new request" solely from an expired lock.

This layered model is the recommended architecture, not an implementation
authorization.

## 21. Minimal Versus Full Reliability Model

### Minimal Phase 10 candidate

```text
stable request key
+ durable request status/reservation
+ characterized deterministic client order ID
+ UNKNOWN state with bounded exchange reconciliation
+ separate symbol-exclusivity policy
```

This candidate is enough to address same-request concurrency and ambiguous ACK
without redesigning the full position ledger. Backend and product choices remain
open.

### Full reliability model

```text
durable open-operation journal
+ request/order/fill/position alias mapping
+ replay/resume across restart and workers
+ desired protection operation linked to open identity
+ lifecycle episode state and outbox/reconciliation
```

The full model belongs to P10-D3/T12 and P10-D5. It should not be forced into a
single T1-B patch.

## 22. Relations To Other Phase 10 Tickets

### T5

D1 provides an open operation identity that protection can reference as
`PROTECTION_PENDING` and `PROTECTED`. P10-01 does not choose queue, synchronous
placement, retry, or protection SLO behavior.

### POS-ID

`request_id` is not `position_id`:

```text
request identity -> one open intent
exchange order/fills -> execution aliases
position identity -> one aggregate lifecycle episode
```

One position episode may involve multiple intentional requests if scale-in is
allowed. One request can also fail without creating a position.

### T12-A / P10-D3

The durable open operation and UNKNOWN state become inputs to open crash
recovery. D1 defines identity and stages; D3 decides compensation, resume, and
cross-resource commit policy.

## 23. Suggested Implementation Ticket Split

These tickets are proposed only for future planning:

| ID | Scope | Current readiness |
|---|---|---|
| P10-D1A Request Identity Contract | producer/caller key, propagation, retry reuse, duplicate return | BLOCKED_BY_PRODUCT_DECISION |
| P10-D1B Durable Reservation | operation schema, atomic claim, lease/fencing, retention | BLOCKED_BY_D1A |
| P10-D1C Exchange Idempotency Contract | client ID constraints, submission/query semantics, sandbox/tests | NEEDS_CHARACTERIZATION |
| P10-D1D Ambiguous ACK Recovery | UNKNOWN transitions, bounded queries, resume/manual override | BLOCKED_BY_D1A_D1C |

No implementation ticket may change production until its predecessor contract
is approved.

## 24. Implementation Guardrails

- Do not use symbol as both request identity and position-exclusivity policy.
- Do not generate a fresh UUID independently on every retry.
- Do not use current position ID as a request key.
- Do not treat lock acquisition as proof that no prior order exists.
- Do not automatically retry UNKNOWN.
- Do not infer client-order-ID limits or query semantics absent repository/API
  characterization.
- Do not add unbounded order-history queries.
- Do not couple D1 to a T5 implementation.
- Preserve current production behavior until product questions are answered.

## 25. Readiness And Conclusion

P10-D1 architecture is **DESIGN AUDITED**.

T1-B implementation readiness is:

**BLOCKED_BY_PRODUCT_DECISION**

The primary blockers are duplicate/scale-in scope, cross-system ownership,
UNKNOWN timeout policy, idempotency retention, and restart guarantee. Exchange
client-order-ID behavior also requires a dedicated repository/API
characterization before implementation.

**P10-01 PASS.** No lock, reservation, request field, client order ID, retry,
position ID change, order parameter change, or production behavior change was
made.
