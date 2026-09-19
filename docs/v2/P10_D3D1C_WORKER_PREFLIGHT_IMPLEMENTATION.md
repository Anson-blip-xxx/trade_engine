# P10-D3D-1C Worker V1/V2 Preflight And Mutation Claim

> Status: **IMPLEMENTED / CLOSED** on the isolated V2 branch. This ticket is
> not a deployment authorization and does not restart or modify running main
> services.

## 1. Scope

This ticket admits an immutable `AlgoProtectionTask` to exchange mutation only
after current episode authority and a Redis mutation claim agree. It adds:

- V1 before any protection cancel;
- one per-slot leased claim with a monotonically increasing fencing token;
- V2 after cancel/query and immediately before create;
- strict Redis command seams with no JSON/file fallback;
- fail-closed typed outcomes and structured local drop logs;
- owner-and-fencing-token checked release.

Legacy four-tuples and malformed tasks remain drop-only in the runtime parser.
FIFO, process-memory queue ownership, no retry/requeue, 11-second task pacing,
and one-second idle polling remain unchanged.

## 2. V1 Admission

`ProtectionTaskFence.acquire` first reads the dedicated slot authority and
requires:

```text
read code = FOUND
status = ACTIVE
complete provenance/episode/generation (`can_mutate_async`)
exchange_position_key = task.exchange_position_key
episode_id = task.episode_id
slot_generation = task.slot_generation
```

The claim acquisition Lua script then atomically rechecks authority revision,
ACTIVE status, episode ID, and slot generation before it creates a lease. This
closes the unlocked read-to-claim race. Missing, malformed, stale, busy, or
unavailable state denies mutation before `_cancel_all_algo`.

## 3. Mutation Claim

The claim is bound to:

```text
exchange_position_key
episode_id
slot_generation
protection_generation
authority_revision
owner_token
fencing_token
```

Redis keys are dedicated and do not reuse `pm:positions`:

```text
pm:protection-claim:v1:<slot-digest>  # leased, default 30 seconds
pm:protection-fence:v1:<slot-digest>  # persistent monotonic token counter
```

Claim acquisition, validation/renewal, and owner-checked release are Lua
scripts. The counter has no TTL and a released/expired claim never causes its
fencing token to be reused.

## 4. V2 Placement Boundary

The existing placement order becomes:

```text
V1 read + claim
exchangeInfo rounding query
cancel/query existing Algo orders
V2 authority + claim validation/renewal
create Algo stop
legacy symbol-only writeback
claim release
```

V2 verifies the same authority revision, ACTIVE episode, slot generation,
claim owner, fencing token, and bound protection generation. Lease loss,
authority change, malformed Redis data, or backend failure yields no create.

The exchange cannot consume the local fencing token, so a process pause after
V2 remains a remote-side residual risk. Reconciliation/operation journaling is
still required for ambiguous effects.

## 5. Deliberately Deferred

- D3D-1D: V3 and conditional `algo_sl_id` writeback by expected authority and
  state revision. The current symbol-only writeback remains and is why this
  ticket must not deploy by itself.
- D3D-2: durable desired-protection state and generation advancement. D3D-1C
  binds `protection_generation` into the claim, but there is no separate
  durable desired-generation authority to compare yet.
- producer integration: active open, break-even, trailing, and legacy lifecycle
  producers still emit the legacy four-argument shape and therefore drop.
- durable queue, replay, operation journal, marker fencing, close fencing, and
  reconcile CAS.

## 6. Deployment Guard

This commit is an isolated V2 intermediate and **must not be deployed by itself**. Until supported producers own canonical ACTIVE authority and D3D-1D
adds V3 writeback CAS, promotion would either drop legacy protection work or
allow an old ACK to pollute symbol-only position state.

No service was restarted, no exchange call was made, and no live Redis/database
state was changed while implementing or testing this ticket.

## 7. Verification

Tests cover:

- exact ACTIVE episode admission and all fail-closed read outcomes;
- atomic claim acquire/busy/release and monotonic fencing tokens;
- backend/malformed response handling;
- authority change and claim loss at V2;
- V1 before cancel and V2 between cancel and create;
- strict Redis seams with no file fallback;
- legacy/malformed queue drop and FIFO/pacing preservation.

## 8. Rollback

Revert the D3D-1C commit. The claim keys are isolated, claims expire, and the
fencing counter is harmless retained high-water. There is no position-schema,
database, or durable queue migration in this ticket.

**P10-D3D-1C PASS.**
