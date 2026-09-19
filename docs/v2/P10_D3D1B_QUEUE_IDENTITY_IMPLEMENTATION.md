# P10-D3D-1B Immutable Queue Episode Identity Extension

> Status: **IMPLEMENTED / CLOSED** on the isolated V2 branch.  This ticket is
> not a deployment authorization and does not restart or modify the running
> main services.

## 1. Scope

This ticket changes only the process-local Algo protection handoff:

- define one frozen queue task carrying the complete canonical identity;
- extend the PM enqueue seam and protection port/service/adapter;
- classify the old four-field tuple as `LEGACY_UNFENCED`;
- drop legacy or malformed work before `place_fn` and therefore before any
  cancel, create, or alias writeback;
- preserve FIFO, process-memory ownership, no retry/requeue, 11-second task
  pacing, and one-second idle polling.

It does not load slot authority, acquire a mutation claim, implement V1/V2/V3,
perform CAS writeback, persist the queue, replay after restart, or change
marker/reconcile/close behavior.

## 2. Producer And Consumer Inventory

Current producers remain:

1. active open in `strategies/shared_executor.py`;
2. legacy lifecycle open in `position_lifecycle/service.py`;
3. break-even replacement in `shared/position_manager.py`;
4. trailing replacement in `shared/position_manager.py`.

The only consumer remains `position_runtime.runtime.algo_worker_loop`; queue
backing and lock remain owned by `shared.position_manager`.

The existing producers do not yet possess active canonical authority and still
emit the legacy four-argument shape. That shape is now deliberately drop-only.
Consequently this isolated V2 commit must not be deployed by itself. D3D-1C
must bind supported producers to current authority and perform worker preflight
before V2 is promoted to the running environment.

## 3. Immutable Payload

`position_protection.task.AlgoProtectionTask` is a frozen value object with:

```text
symbol
side
trigger_price
qty
exchange_position_key
episode_id
slot_generation
protection_generation
```

Construction rejects non-BUY/SELL sides, nonpositive/nonfinite trigger or
quantity, non-UUID episode IDs, nonpositive generations, noncanonical slot
objects, and symbol/slot mismatch. No identity is derived from symbol,
`position_id`, current Redis state, or dequeue-time state.

## 4. Compatibility Classification

```text
AlgoProtectionTask -> FENCED
legacy 4-tuple     -> LEGACY_UNFENCED
anything else      -> MALFORMED
```

Both non-fenced classifications are logged once and dropped before `place_fn`.
They are not upgraded, retried, or relabeled as the current episode.

## 5. Deliberately Deferred

- D3D-1C: authority read, ACTIVE/slot/episode/generation preflight and mutation
  claim before cancel/create;
- D3D-1D: conditional `algo_sl_id` writeback by expected authority/revision;
- D3D-2: durable desired-protection generation advancement and replacement
  ordering;
- durable operation journal and restart replay.

## 6. Verification Contract

Tests cover frozen identity, malformed input, symbol/slot mismatch, complete
enqueue construction, partial identity rejection, service forwarding, fenced
task dispatch, legacy/malformed pre-exchange drop, and the historical ABA
characterization changing from unsafe mutation to explicit drop.

Phase 10 guards continue to prove that worker authority lookup, mutation
permission, CAS writeback, durable queue, replay, and active position schema
wiring are absent.

## 7. Rollback

Revert the D3D-1B commit. No Redis migration or durable queue migration is
needed because the queue is process-local and the active position schema is
unchanged.

**P10-D3D-1B PASS.**
