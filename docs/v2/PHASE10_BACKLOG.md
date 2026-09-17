# Phase 10 Reliability Backlog

> Imported exactly from `PHASE9_CLOSURE.md`. P10-00 is architecture-only;
> production change is not allowed for any row below.

## Status Vocabulary

`READY`, `READY_FOR_DESIGN`, `READY_FOR_IMPLEMENTATION`, `NEEDS_DESIGN`,
`NEEDS_PRODUCT`, `NEEDS_CHARACTERIZATION`, `BLOCKED_BY_OTHER_TICKET`,
`BLOCKED_BY_PRODUCT_DECISION`, `BLOCKED_BY_D3`, and `DESIGN AUDITED`.

<!-- P10_BACKLOG_START -->
| ID | Cluster | Status | depends_on | decision_required | target_artifact | production change allowed? |
|---|---|---|---|---|---|---|
| T1-B | A Open Safety | BLOCKED_BY_PRODUCT_DECISION | P10-D1 DESIGN AUDITED | duplicate scope, scale-in, cross-system/side policy, UNKNOWN timeout policy, retention and restart guarantee | approved product contract, then D1A-D1D implementation tickets | NO |
| T5 | A Open Safety | BLOCKED_BY_PRODUCT_DECISION | P10-D2 DESIGN AUDITED; implementation identity from P10-D5 | verified fill-to-protection SLO, open/admission semantics, failure action, replacement gap/overlap, restart guarantee | approved product contract, then D2A-D2E implementation tickets | NO |
| PMB-23A | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION | P10-D4/D5 DESIGN AUDITED; implementation P10-D3 | duplicate-versus-loss preference and authoritative close recorder sink | approved durability contract, then D3 ghost-close replay design | NO |
| T12 | B Close/Reconcile Durability | READY_FOR_DESIGN | P10-D1/D2/D4/D5 DESIGN AUDITED; P10-D3 next | integrated authority, acknowledgement, compensation, and restart guarantees | P10-D3 T12-A/B/C/D crash-consistency model | NO |
| PMB-26C1 | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION | P10-D4 DESIGN AUDITED | PG event ledger required durability versus explicit best-effort audit | approved event durability contract, then D4B/D3 | NO |
| PMB-26C2 | B Close/Reconcile Durability | BLOCKED_BY_D3 | PMB-26C1; PMB-26B2; P10-D3 | partial-commit isolation, retry, and compensation after selected C1/B2 policy | D3 staged partial-commit and replay model | NO |
| PMB-24 | D Product Policy | NEEDS_PRODUCT | product decision; implementation later P10-D3/D5 | internal repair, business close, or discrepancy quarantine | reconcile divergence policy | NO |
| PMB-26B2 | D Product Policy | NEEDS_PRODUCT | product decision; implementation later P10-D4/C2 | at-most-once, at-least-once, bounded retry, or escalation | notification acknowledgement/retry policy | NO |
| POS-ID | C Identity/Marker Model | BLOCKED_BY_PRODUCT_DECISION | P10-D5 DESIGN AUDITED; D1/D2 relations defined | episode boundaries, scale-in/side-flip, cross-system ownership, restart guarantee, legacy migration | approved episode/alias/migration contract, then D5A-D5C | NO |
| PMB-4 | C Identity/Marker Model | BLOCKED_BY_PRODUCT_DECISION | P10-D5 DESIGN AUDITED; POS-ID decision | marker role separation, cross-episode cooldown scope, retention/expiry, malformed policy | approved marker contract, then PMB-4A/B/C and D5D | NO |
<!-- P10_BACKLOG_END -->

## Cluster Ownership

| Cluster | Design owner | Product owner |
|---|---|---|
| A Open Safety | Execution/Lifecycle/Protection | trading safety/product |
| B Close/Reconcile Durability | State/Reconcile/Ledger | accounting/reliability product |
| C Identity/Marker Model | State/Ledger architecture | data ownership/product |
| D Product Policy | implementation owner follows decision | operations/product |

## Design Tickets

| ID | Status | Output | Enables |
|---|---|---|---|
| P10-D1 Open Idempotency Model | **DESIGN AUDITED** | `P10_D1_OPEN_IDEMPOTENCY_MODEL.md`; active path + request identity + broker acknowledgement contract | T1-B, T12-A, T5 identity |
| P10-D2 Protection Establishment Model | **DESIGN AUDITED** | `P10_D2_PROTECTION_ESTABLISHMENT_MODEL.md`; PROTECTED definition + generation + SLO/failure/retry/restart contract | T5, T12-A, P10-D5 identity requirements |
| P10-D3 Crash Consistency Model | **READY_FOR_DESIGN** | lifecycle saga/stages + compensation/recovery using D1/D2/D4/D5 | T12, PMB-23A, C2, PMB-24 implementation |
| P10-D4 Persistence Failure Policy | **DESIGN AUDITED** | `P10_D4_PERSISTENCE_FAILURE_POLICY.md`; sink roles + acknowledgement + required/best-effort/retry/UNKNOWN + D3 input contract | C1, C2, PMB-23A, B2, T12-C design |
| P10-D5 Position Identity And Marker Authority | **DESIGN AUDITED** | `P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md`; identity taxonomy + episode/reopen semantics + marker authority/migration/fencing model | POS-ID, PMB-4, D2 generation, T12/D3 replay identity |

## Recommended Order

1. P10-D1 as P10-01.
2. P10-D2 product SLO and timing characterization in parallel.
3. P10-D5 identity and marker authority.
4. P10-D4 persistence and delivery policy.
5. P10-D3 integrated crash/restart model.
6. Behavior tickets only after their predecessor artifacts are approved.

No imported backlog ticket is implementation-ready at P10-00.

## P10-D1 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D1A | stable request identity contract and propagation | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D1B | durable reservation/state and ownership | BLOCKED_BY_D1A | NO |
| P10-D1C | Binance client-order-ID submission/query contract | NEEDS_CHARACTERIZATION | NO |
| P10-D1D | UNKNOWN acknowledgement recovery and resume | BLOCKED_BY_D1A_D1C | NO |

P10-D1 is design-complete, but T1-B remains
`BLOCKED_BY_PRODUCT_DECISION`. See
`docs/v2/P10_D1_OPEN_IDEMPOTENCY_MODEL.md`.

## P10-D2 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D2A | durable desired-protection episode/generation record | BLOCKED_BY_D5 | NO |
| P10-D2B | durable handoff, ownership, restart replay, and backpressure | BLOCKED_BY_D2A | NO |
| P10-D2C | exchange Algo ACK/status/query contract | NEEDS_CHARACTERIZATION | NO |
| P10-D2D | generation-fenced reconcile and UNKNOWN recovery | BLOCKED_BY_D2A_D2C | NO |
| P10-D2E | SLO, admission, health, and emergency failure behavior | BLOCKED_BY_PRODUCT_DECISION | NO |

P10-D2 is design-complete, but T5 remains
`BLOCKED_BY_PRODUCT_DECISION` and implementation depends on P10-D5. See
`docs/v2/P10_D2_PROTECTION_ESTABLISHMENT_MODEL.md`.

## P10-D5 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D5A | exchange slot and immutable episode boundary/ownership contract | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D5B | request/order/fill/legacy/protection alias model | BLOCKED_BY_D5A_AND_API_CHARACTERIZATION | NO |
| P10-D5C | versioned live/historical migration and provenance | BLOCKED_BY_D5A_AND_PRODUCT_DECISION | NO |
| P10-D5D | typed marker roles, lifecycle generation, and compare-and-clear | BLOCKED_BY_D5A_AND_PRODUCT_DECISION | NO |

## PMB-4 Decomposition

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| PMB-4A | stale marker retention and logical/physical expiry policy | BLOCKED_BY_PRODUCT_DECISION | NO |
| PMB-4B | cross-episode contamination and marker ABA fencing | BLOCKED_BY_POS-ID | NO |
| PMB-4C | malformed/version handling, observability, and safe cleanup | NEEDS_DESIGN; implementation waits D5D | NO |

P10-D5 is design-complete, but POS-ID and PMB-4 remain
`BLOCKED_BY_PRODUCT_DECISION`. P10-D4 is now audited below; P10-D3 is next.
See `docs/v2/P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md`.

## P10-D4 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D4A | Redis/file operational-state acknowledgement and revision contract | BLOCKED_BY_PRODUCT_DECISION_AND_D3 | NO |
| P10-D4B | PG event required-versus-best-effort audit contract | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4C | authoritative close/partial accounting and finalization ACK | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4D | ClickHouse loss policy and notification delivery acknowledgement | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4E | durable operation journal stage/backend/replay model | READY_FOR_D3_DESIGN | NO |

## P10-D4 Readiness

| Ticket | Readiness |
|---|---|
| PMB-23A | BLOCKED_BY_PRODUCT_DECISION; implementation also needs D3 |
| PMB-26C1 | BLOCKED_BY_PRODUCT_DECISION |
| PMB-26C2 | BLOCKED_BY_D3 after C1/B2 decisions |
| T12-C | BLOCKED_BY_D3 |
| P10-D3 | READY_FOR_DESIGN as P10-05 |

P10-D4 is design-complete. No persistence-policy behavior ticket is ready for
implementation. See `docs/v2/P10_D4_PERSISTENCE_FAILURE_POLICY.md`.
