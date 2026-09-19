# P10 R2 / D2A - Durable Desired Protection Record

> **IMPLEMENTED / CLOSED as dormant V2 infrastructure.**
> **ACTIVE RUNTIME BEHAVIOR = 0 CHANGE.**

## 1. Outcome

R2 adds the durable desired-protection authority needed to distinguish what
protection should exist from what the exchange currently proves exists. It
provides a versioned record, monotonic generation within an episode, strict
state transitions, per-slot Redis Lua CAS, typed failures, and operation/spec
idempotency.

It does not enqueue, cancel, create, query, retry, reconcile, or verify exchange
Algo orders. It does not modify `pm:positions`, `algo_sl_id`, the in-memory
queue, or worker behavior.

## 2. Production Modules

```text
position_protection/desired.py
position_protection/desired_redis.py
```

The Redis dependency is injected. Import performs no Redis/network/file IO and
starts no thread.

## 3. Authority Boundaries

- Binance remains physical exposure and observed active-protection truth.
- `pm:slot:v1:*` remains episode/slot-generation authority.
- `pm:position-projection:v1:*` remains canonical operational projection.
- `pm:desired-protection:v1:*` is authoritative only for what protection is
  desired for the current episode.
- Exchange `algoId` values are recovery aliases, not ownership, generation, or
  proof of `PROTECTED`.

R2 never describes an ACK, queue item, worker pickup, alias, or local writeback
as `PROTECTED`. D2C exchange characterization is still required before an
active caller may establish `ACTIVE`.

## 4. Record Schema

`DesiredProtectionRecord` is frozen with `schema_version = 1`:

```text
exchange_position_key
episode_id
slot_generation
protection_generation
desired_intent_id
trigger_price
covered_quantity
closing_side
status
exchange_algo_aliases
revision
last_operation_id
created_at
updated_at
schema_version
```

The parser requires exact fields and validates the complete embedded slot,
UUID, positive generations/revision/price/quantity, BUY/SELL close side, unique
nonempty aliases, timestamps, and status enum.

No API credential, wallet, webhook payload, or exchange secret is stored.

## 5. Generation And Idempotency

For one episode:

```text
initial desired stop       protection_generation=1
same spec retry            remains generation=1
new trigger/quantity/side  generation=2
status or alias update     generation unchanged; revision +1
```

An unchanged canonical desired spec returns `ALREADY_APPLIED`, even when a
caller retries under another operation ID. Reusing one desired intent ID for a
different spec is `CONFLICT`. Concurrent changed specs from one revision have
exactly one winner.

On a later episode with higher slot generation, protection generation restarts
at 1. Episode ID and slot generation prevent old work from mutating the new
record. Record revision also restarts at 1 because it is episode-scoped; the
slot authority generation is the cross-episode ABA fence.

## 6. State Machine

The schema contains D2's semantic states:

```text
NONE PENDING SUBMITTING UNKNOWN ACTIVE REPLACING
FAILED CANCELLED SUPERSEDED
```

Legal transition validation follows the audited D2 candidate state machine.
Ambiguous mutation moves to `UNKNOWN`, never directly to ordinary retry success.
Terminal `FAILED`, `CANCELLED`, and `SUPERSEDED` records do not reopen through a
status CAS. A new desired spec uses a new protection generation.

Same-status CAS is allowed only for a real alias repair. It cannot be used to
manufacture revision churn without changing recovery evidence.

This is storage capability, not exchange verification. No production caller in
R2 transitions a record to `ACTIVE`.

## 7. Redis CAS

One current desired record is stored per physical slot:

```text
pm:desired-protection:v1:<sha256(canonical-slot-key)>
```

Lua atomically validates and compares:

```text
expected episode_id
expected slot_generation
expected protection_generation
expected revision
last_operation_id
canonical desired spec and legal transition
```

It then performs one SET or returns without mutation. There is no TTL, delete,
lock, file fallback, whole-snapshot fallback, or `pm:positions` double-write.
Malformed/partial/semantically invalid records are preserved.

## 8. Typed Results

Reads return `FOUND`, `NOT_FOUND`, `MALFORMED`, or `UNAVAILABLE`.

Writes return `APPLIED`, `ALREADY_APPLIED`, `STALE`, `CONFLICT`, `NOT_FOUND`,
`MALFORMED`, `INVALID`, `UNAVAILABLE`, or `UNKNOWN`.

`UNAVAILABLE` is used only when a pre-attempt probe proves Lua was not invoked.
An exception from the Lua call is `UNKNOWN` because commit acknowledgement may
have been lost. There is no legacy fallback in either case.

## 9. QA Evidence

Tests cover:

- exact frozen schema and serialization;
- generation/revision separation;
- same-spec and same-operation idempotency;
- changed-spec allocation and desired-intent conflict;
- legal, illegal, terminal, UNKNOWN, and alias-repair transitions;
- concurrent generation allocation with exactly one winner;
- stale revision and cross-episode ABA rejection;
- new-episode generation reset with higher slot generation;
- malformed, partial, and semantically invalid record preservation;
- `UNAVAILABLE` versus ambiguous `UNKNOWN`;
- deterministic namespace, no TTL/delete/fallback, and import-time zero IO;
- real Lua execution in disposable Redis over a temporary Unix socket with TCP
  and persistence disabled;
- Phase 10 non-wiring architecture facts, PositionManager, execution, and full
  repository regression.

## 10. Remaining Gates

R2 closes only D2A. It does not close:

- D2B durable handoff/replay/backpressure;
- D2C Algo ACK/query/status characterization;
- D2D UNKNOWN recovery and generation-aware reconciliation;
- D2E SLO, admission, replacement gap/overlap, and emergency policy;
- R3/D3D-1D conditional alias writeback.

R3 may now implement the dormant/isolated conditional writeback contract, but
must not claim that alias persistence proves `PROTECTED`.

**P10 R2 PASS.**
