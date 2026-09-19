# P10 R3 / D3D-1D - V3 Conditional Protection Alias Writeback

> **IMPLEMENTED / CLOSED on isolated V2.** Existing legacy producers remain
> unfenced/dropped; R4 producer wiring is still required before deployment.

## Outcome

R3 replaces symbol-only ACK writeback for fenced work with one Redis Lua
transaction that validates slot authority, the live mutation claim, desired
protection generation/revision, and canonical projection revision before
appending an exchange Algo alias.

The writeback does not set desired state to `ACTIVE` and does not claim that an
ACK proves `PROTECTED`. It keeps `SUBMITTING`; D2C/D2D must verify exchange state.

## V3 Task

`ConditionalWritebackProtectionTask` extends the V2 immutable queue identity
with `desired_revision` and `projection_revision`. Both revisions must be
present together. V1 acquisition and V2 pre-create validation remain unchanged.

## Atomic V3 Check

`RedisConditionalAliasWritebackAdapter` reads four keys in one Lua invocation:

```text
pm:slot:v1:<slot>
pm:protection-claim:v1:<slot>
pm:desired-protection:v1:<slot>
pm:position-projection:v1:<slot>
```

It compares authority revision/status, claim owner/fencing token, episode, slot
generation, protection generation, desired revision, and projection revision.
Only then does it append the alias and increment desired revision. The canonical
projection is checked but not mutated; the alias belongs to desired protection.

Same operation retry after revision increment returns `ALREADY_APPLIED`. Lost
claims return `CLAIM_LOST`; old episode/generation/revision returns `STALE`;
ambiguous Redis acknowledgement returns `UNKNOWN`. None falls back to
`pm:positions` or symbol-only `_save()`.

## Worker Boundary

Fenced V2/V3 execution invokes `_algo_place_sl_inner` with
`legacy_writeback=False`. V3 supplies the atomic ACK callback. V2 has no callback
and therefore performs no unsafe writeback. Direct legacy calls retain their
existing compatibility writeback; R4 decides their migration/removal.

No producer currently emits V3 work, so this commit does not enable new opens,
cancel/create behavior, or production migration.

## QA

Tests cover real disposable Redis Lua execution, applied/retry idempotency,
claim loss, desired/projection stale revisions, UNKNOWN, immutable V3 identity,
revision pairing, no legacy fallback, V1/V2 ordering, and legacy golden parity.
Repository gates include Phase 10, PositionManager, execution, full pytest,
Ruff, and diff checks.

**P10 R3 PASS.**
