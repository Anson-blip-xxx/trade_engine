# P10-06A / P10-D3D - Generation Fencing Implementation Audit

> Architecture and characterization only. Production code is unchanged. This
> audit finds the smallest generation-fencing boundary that can prevent work
> created for an old position episode from mutating a later same-symbol episode.

## 1. Scope And Safety Invariant

The immediate invariant is:

```text
Work created for episode A must never cancel, create, clear, remove, or write
state for later episode B, regardless of how late A's work executes.
```

P10-06A prioritizes fencing stale work before building the full D3 operation
journal and recovery coordinator. It does not solve lost work.

This audit does not authorize:

- changing `position_id` or adding a production episode field;
- changing the protection queue or marker payload;
- changing Redis position state;
- adding a journal, replay, retry, or generation counter;
- changing open, close, protection, marker, monitor, or reconcile behavior.

## 2. Current Queue And Worker Contract

The exact process-local queue contract is:

```text
_ALGO_QUEUE = []
item = (symbol, side, trigger_price, qty)
dequeue = queue.pop(0)
```

| Field | Present | Identity meaning |
|---|---:|---|
| `symbol` | yes | reusable exchange slot lookup only |
| close-order `side` | yes | BUY/SELL mutation parameter, not episode identity |
| `trigger_price` | yes | desired value from enqueue time, mutable across replacements |
| `qty` | yes | desired value from enqueue time, mutable after partial close |
| `position_id` | no | discarded even where available |
| `position_episode_id` | no | absent |
| `protection_generation` | no | absent |
| `request_id` | no | absent |
| enqueue time/attempt/owner | no | absent |

The worker pops the item before placement, destructures four fields, calls
`_algo_place_sl_inner`, then sleeps 11 seconds. It does not reload position
identity, retry, requeue, or classify stale work.

FIFO is scheduling, not correctness. It does not protect against duplicate
enqueue, two process-local queues, delayed exchange calls, close/reopen, or
future replay. Restart destroys the ordering history and every pending item.

## 3. Placement Mutation Sequence And Blast Radius

`_algo_place_sl_inner(symbol, side, trigger_price, qty)` performs:

```text
best-effort exchangeInfo rounding
-> cancel all NEW/WORKING/TRIGGERED algo orders for symbol
-> POST one reduce-only STOP_MARKET using queued side/trigger/qty
-> accept any dict containing algoId
-> load current pm:positions
-> if symbol exists, write algo_sl_id into that current row
-> save the whole position snapshot
```

There is no position load or identity comparison before cancel or create. The
only load occurs after exchange mutation and checks only `symbol in positions`.

For stale task A when episode B currently owns the symbol, the blast radius is:

1. B's active protection can be cancelled by A's symbol-wide cancel-all.
2. A's stale side, trigger, and quantity can create an order against B's slot.
3. If sides differ, create may reject, but B was already unprotected by cancel.
4. If sides match, an incorrectly priced or sized stop can become active for B.
5. A's returned `algoId` is written into B's metadata by symbol alone.
6. The final whole-snapshot save can also overwrite unrelated concurrent state.

The first five are episode/generation fencing defects. The sixth is a separate
stale-snapshot read-modify-write defect.

## 4. Stale Work Taxonomy

| Category | Current payload/identity | Current status | Generation-fencing relation |
|---|---|---|---|
| A. queued protection create | four-tuple: symbol, side, trigger, qty | **ACTIVE** | old episode can cancel/create/write into current symbol row |
| B. queued protection replace | same four-tuple; caller retains mutable `pos`, queue does not | **ACTIVE** | no episode or protection generation; old desired stop can supersede new |
| C. delayed protection retry | none; worker has no retry/requeue | **NOT ACTIVE** | future retry must carry the same episode and generation |
| D. delayed marker clear | synchronous call with symbol only after blocking close/merge work | **ACTIVE** | old operation can unconditionally delete a newer symbol marker |
| E. delayed close result | symbol + old `pos` + old whole snapshot; synchronous exchange call | **ACTIVE** | old completion can reduce, update, or pop a later episode; also snapshot RMW |
| F. delayed reconcile result | symbol-keyed local/exchange snapshots | **STRUCTURAL** | `reconcile_all` has no in-repo production caller, but would save stale state if invoked |
| G. monitor callback | symbol + mutable old position/snapshot; WS row has no episode ID | **ACTIVE** | polling and WS work can complete after the symbol's identity changes |
| H. ghost cleanup callback | symbol + old `pos` + old snapshot; symbol lock | **ACTIVE** | flat evidence and callback payload can become stale before record/pop/mark |

`ACTIVE` does not imply that every path uses an asynchronous language primitive.
Blocking synchronous exchange/ledger calls are delayed work because another
process or the WS thread can mutate the same account and Redis state meanwhile.

## 5. Deterministic ABA Reproduction

The protection ABA is reproducible with current runtime seams; it is not only a
structural possibility:

```text
1. state contains BTC episode A
2. enqueue (BTC, SELL, trigger_A, qty_A)
3. episode A closes; queue item is not removed
4. state receives BTC episode B with position_id B and algo_sl_id B
5. run one worker iteration
6. worker sees only BTC/SELL/trigger_A/qty_A
7. it cancels by BTC, creates from A fields, then writes returned algoId to B
```

The characterization golden uses the real queue append, runtime FIFO pop, and
placement orchestration while replacing only exchange/state infrastructure. It
asserts that B keeps its own `position_id` but receives A's `algoId`, proving
that no comparison occurred.

The repository's four-hour recent-close policy does not fence this race:

- the queue is unbounded and can remain delayed;
- external/manual exposure can reopen independently;
- exchange-visible exposure self-clears the symbol marker;
- the marker has no episode identity and may be blindly cleared;
- correctness cannot depend on a policy timeout.

## 6. Protection Create Risk

Initial active-path protection is enqueued after the position row and its
`position_id` are produced. Nevertheless the enqueue API accepts only four
fields and discards that available identity.

An old create task is not a safe no-op after full close. With no current local
position it still performs cancel-all and POST; only alias writeback is skipped.
If a same-symbol episode exists, it performs all three harmful mutations:
cancel, create, and current-row writeback.

Missing local state must therefore mean `STALE -> DROP`, not blind submit.

## 7. Protection Replace Risk

There are two active replacement producers:

| Producer | Current ordering | Fence missing |
|---|---|---|
| break-even `_update_stop_loss` | cancel recorded old ID, enqueue, mutate local `sl`/`be_done` | episode and protection generation |
| trail/peak `_place_trail_sl` | save new local `sl`, cancel all, enqueue; worker cancels all again | episode and protection generation |

Every replacement for one episode has the same current `position_id`. Therefore
position equality alone cannot distinguish:

```text
generation 7: stop 90
generation 8: stop 95
```

If task 7 executes after task 8, episode equality passes while stale desired
state overwrites current protection. Replacement correctness requires a
monotonic `protection_generation` in addition to an episode fence.

## 8. Full-Close And Reopen Races

### Full close, no reopen yet

Full close cancels exchange algo orders after confirmed flatness and removes
local state, but it neither purges nor invalidates `_ALGO_QUEUE`. A pending task
can later cancel symbol orders and POST a stale stop. It skips local writeback
only when the symbol is still absent.

Current result: **not a safe no-op**.

### Same-symbol reopen

When B occupies the symbol before A's task runs, symbol identity cannot
distinguish the episodes. Side is also insufficient: two sequential episodes
may have the same side, and one-way exchange mode reuses the same `BOTH` slot.

Current answer:

```text
Is symbol-only identity sufficient to distinguish episode A from B? NO.
```

## 9. Marker, Close, Monitor, Ghost, And Reconcile Relation

### Marker clear

`clear_closed_marker(symbol)` is an unconditional symbol-key delete. There is no
dedicated delayed marker-clear queue, so a standalone async marker worker is
**NOT ACTIVE**. However delayed synchronous close and merge operations do call
blind clear after earlier reads/writes. This is an **ACTIVE marker ABA risk**.

D3D-3 must compare marker type, episode, and operation token/generation. It is
separate from the first protection-queue fence.

### Close result

Close exchange calls are synchronous, with no callback/future result path.
There is therefore no separate async close-result subsystem. The practical race
is still active: blocking calls retain old `pos` and whole `positions`, then
mutate/pop/save without reloading and comparing the current episode.

### Monitor and ghost callbacks

Polling monitor callbacks receive symbol, mutable old `pos`, and a whole old
snapshot. WS close callbacks hold old symbol metadata without episode identity.
Ghost cleanup uses a symbol lock but obtains exchange-flat evidence before the
per-symbol lock and never compares current `position_id`. These need lifecycle
fencing, but expanding D3D-1 to them would multiply scope and dependencies.

### Reconcile

`reconcile_all` loads local state, performs exchange IO, pops by symbol, and
saves the whole old snapshot. No in-repository production caller was found, so
its generation risk is **STRUCTURAL**. Its ordinary stale-snapshot overwrite is
a distinct version/CAS problem covered by proposed D3D-4.

## 10. Identity Available At Enqueue

| Identity/data | Active S6/S8 open | Legacy lifecycle open | Replace paths |
|---|---:|---:|---:|
| symbol | yes | yes | yes |
| side | yes | yes | yes |
| trigger/qty | yes | yes | yes |
| system | available in caller/state | available input | available in `pos` |
| entry/open time | state exists before enqueue | constructed after enqueue | available in `pos` |
| current `position_id` | **yes before enqueue** | usually **no** before enqueue | may exist in `pos` |
| canonical episode ID | no | no | no |
| protection generation | no | no | no |
| request ID | no | no | no |

Active S6/S8 calls `_update_pos_cache`, receives `position_id`, records the PG
event, notifies, then enqueues. The queue signature discards the ID.

Legacy `PositionLifecycleService.open_position` enqueues before constructing and
saving its position row. Metadata may later supply a `position_id`, but it is not
an invariant at enqueue time. Active and legacy paths therefore cannot adopt the
same temporary fence without first resolving this compatibility difference.

## 11. Can Current position_id Be A Temporary Fence?

| Property | Assessment |
|---|---|
| available before active enqueue | yes |
| normally changes on active same-symbol reopen | yes, wall-clock component changes |
| readable from current position state | yes when persistence survives |
| stable across restart | only when explicit metadata survives |
| reconstructable from exchange | no |
| consistent in legacy open | no |
| canonical/collision-proof | no |
| distinguishes same-episode protection replacements | no |
| closes check-to-exchange-mutation race | no |

Conclusion:

**Current `position_id` can be a defense-in-depth temporary A-vs-B check for the
active path, but it is not suitable as the authoritative D3D episode fence.**

It is generated after exchange submission from system, symbol, entry, and local
wall time. Legacy, merge, and fallback paths have absent or different formats.
A single pre-mutation equality check also cannot make remote cancel/create atomic
with a concurrent episode transition.

## 12. Fence Candidate Comparison

| Candidate | Availability now | ABA safety | Migration | Complexity | Assessment |
|---|---:|---:|---:|---:|---|
| A. current `position_id` equality | active path yes; legacy inconsistent | partial | mixed legacy formats | low | temporary defense only, not authority |
| B. state version counter | absent | detects state change, but does not identify episode alone | state schema migration | medium | useful projection CAS, insufficient alone |
| C. generated episode token | absent | strong across reopen if durable/current | canonical D5 migration needed | medium/high | required D3D-1 authority |
| D. protection generation | absent | strong for replacement ordering within one episode | desired-protection migration | medium/high | required D3D-2, not an episode substitute |
| E. symbol+entry+opened-at fingerprint | partially available | heuristic/collision/reconstruction risk | format drift | low | rejected as correctness fence |

The minimum correct identity is not a larger fingerprint. It is an opaque,
immutable episode token plus a monotonic desired-protection generation.

## 13. Episode Generation Versus Protection Generation

These dimensions must remain separate:

```text
exchange slot BTC
  episode generation 41 -> episode A
  flat
  episode generation 42 -> episode B

episode B
  protection generation 1 -> initial stop
  protection generation 2 -> break-even
  protection generation 3 -> trailing stop
```

- Episode identity/generation rejects all A work once B owns the slot.
- Protection generation rejects older desired stops within B.
- Retry of the same desired protection keeps the same protection generation.
- A new trigger or approved quantity change increments protection generation.
- One counter must not be overloaded to mean both lifecycle ownership and
  desired protection revision.

## 14. Recommended Minimal Implementation Scope

The first behavior ticket should remain narrow:

**D3D-1 Protection Queue Episode Fence**

Scope:

- protection tasks produced by active and supported legacy open/replace paths;
- carry one authoritative expected episode token;
- load and compare current ownership before any cancel/create exchange mutation;
- reject missing/mismatched identity as stale;
- condition alias writeback on the same episode;
- log and count stale drops.

Out of scope:

- durable queue/restart replay;
- exchange UNKNOWN recovery;
- marker compare-and-clear;
- close/ghost/monitor/reconcile fencing;
- protection replacement generation ordering;
- market-close compensation.

Narrow scope limits blast radius, but implementation still requires an approved
D5 episode authority. A best-effort active-path `position_id` patch would not
satisfy the stated invariant and is not recommended as D3D-1 completion.

## 15. Future Queue Schema And Validation

Candidate shape:

```text
(
  symbol,
  side,
  trigger_price,
  qty,
  episode_token,
  protection_generation,
)
```

Before destructive exchange work:

```text
current = load_current_position(symbol)
if current is missing:
    STALE -> drop + log + metric
if current.episode_token != task.episode_token:
    STALE -> drop + log + metric
if current.protection_generation != task.protection_generation:
    STALE -> drop + log + metric
revalidate ownership immediately before cancel/create
perform exchange mutation under the approved slot ownership protocol
compare episode and generation again before conditional alias writeback
```

A post-create mismatch cannot undo a harmful earlier cancellation. Validation
must therefore occur before cancel, before create where the ownership protocol
requires it, and at writeback. D3D-1 design must pair the token with a slot
serialization/fencing primitive rather than rely on one unlocked read.

## 16. Missing Identity And Fail-Safe Direction

The candidate D3D rule is explicit:

```text
identity missing, malformed, unavailable, or mismatched
=> do not cancel
=> do not create
=> do not write algo_sl_id
=> classify STALE/UNVERIFIABLE and DROP the in-memory item
```

Executing unfenced work is prohibited because it can remove protection from the
wrong episode. Drop can leave the current episode unprotected, but D3D must not
silently solve that by mutating an unverified owner. D2 health/admission and
emergency policy owns detection, retry from authoritative desired state, open
halt, quarantine, or compensation close.

D3D never initiates a market close by itself.

## 17. Restart, Journal, And Durability Relation

Current restart creates an empty queue. Generation fencing solves stale
execution; it does not recover pending work:

```text
D3D stale suppression != D3A journal != D3B restart replay
```

A future journal may durably hold episode, protection generation, desired spec,
and operation stage. The execution-time ownership comparison remains necessary
even with a journal because a paused old worker can resume after a newer owner
or episode wins.

Fencing can be specified independently, but implementation needs a durable
episode authority. Durable replay additionally needs D3A/D3B.

## 18. Backward Compatibility And Deployment

Options for an old four-tuple item:

| Option | Result | Decision |
|---|---|---|
| A. drop legacy item | no unfenced exchange mutation; possible protection gap | **selected** |
| B. execute as unfenced legacy | preserves behavior but violates the invariant | rejected |
| C. derive current identity at execution | relabels old A work as B and defeats fencing | rejected |
| D. compatibility parser | can recognize old shape but still must drop it | acceptable parser behavior |

Because the queue is process memory, a normal process-restart deployment
naturally removes old four-tuples. There is no persisted cross-version queue to
migrate. A hot reload that preserves process memory must drain/drop old items
before starting a fenced worker; executing them unfenced is not compatible.

This natural queue loss simplifies payload rollout but is not a recovery
feature. Existing unprotected positions still require D2 operational handling.

## 19. Logging And Metrics

Future stale drop should emit structured local observability only:

- count by reason: missing position, episode mismatch, generation mismatch,
  malformed legacy item, or state unavailable;
- task symbol, expected/current opaque IDs, and generations where safe;
- queue age when available;
- counter and age gauge for protection health/admission.

Do not send Telegram or PG writes from the stale-drop hot path. Those auxiliary
sinks can delay the safety decision and are not protection authority.

Current code has no stale-task metric or explicit stale/superseded log.

## 20. Implementation Splits And Readiness

| ID | Scope | Readiness |
|---|---|---|
| D3D-1 | Protection Queue Episode Fence | `BLOCKED_BY_D5_PRODUCT_DECISION` |
| D3D-2 | Protection Generation Fence | `BLOCKED_BY_D2_PRODUCT_DECISION` |
| D3D-3 | Marker Compare-and-Clear Fence | `BLOCKED_BY_D5_PRODUCT_DECISION` |
| D3D-4 | Reconcile Version Fence | `BLOCKED_BY_D5_PRODUCT_DECISION_AND_D4_ACK_CONTRACT` |

D3D-1 is technically isolated and should be the first implementation after D5
selects canonical episode authority and legacy active-position treatment. It is
not `READY_FOR_IMPLEMENTATION` today because current `position_id` does not
satisfy that authority contract across active, legacy, restart, reconstruction,
and concurrent transition paths.

D3D-2 separately depends on D2 decisions for when trigger/quantity changes
advance generation and replacement gap/overlap semantics. D3D-3 and D3D-4 must
not be bundled into D3D-1.

## 21. Implementation Guardrails

- Do not use symbol, side, `algoId`, trigger, quantity, entry, or open time as
  episode identity.
- Do not credit FIFO ordering as stale-work prevention.
- Do not execute a legacy item by deriving the current episode at dequeue time.
- Do not check identity only after cancel/create.
- Do not use episode equality as protection replacement ordering.
- Do not combine episode generation and protection generation.
- Do not add queue durability or replay in D3D-1.
- Do not expand D3D-1 into marker, close, monitor, ghost, or reconcile changes.
- Do not market-close from a stale-task rejection.
- Do not claim `position_id` equality is a canonical D5 episode contract.

## 22. Audit Result

| Exit criterion | Result |
|---|---|
| production diff | zero |
| stale-work taxonomy | complete |
| queue schema | frozen as four-tuple |
| deterministic ABA evidence | PASS |
| create/replace blast radius | mapped |
| full-close/reopen race | mapped |
| marker/close/reconcile relation | classified |
| current identity availability | mapped |
| `position_id` suitability | temporary defense only; not authority |
| fence candidates | compared |
| episode/protection generations | separated |
| minimal implementation scope | D3D-1 isolated |
| future queue/validation contract | defined |
| FIFO/restart/journal relation | defined |
| compatibility/deployment | legacy item drop selected |
| fail-safe direction | unverifiable means drop before mutation |
| readiness | explicit |

**P10-06A PASS.**

P10-D3D generation-fencing design is **DESIGN AUDITED**. The first implementation
boundary is the protection queue, but D3D-1 remains
`BLOCKED_BY_D5_PRODUCT_DECISION` until canonical episode authority and legacy
position treatment are approved. No production behavior or schema changed.
