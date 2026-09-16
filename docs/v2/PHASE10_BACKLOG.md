# Phase 10 Reliability Backlog

> Imported exactly from `PHASE9_CLOSURE.md`. P10-00 is architecture-only;
> production change is not allowed for any row below.

## Status Vocabulary

`READY`, `NEEDS_DESIGN`, `NEEDS_PRODUCT`, `NEEDS_CHARACTERIZATION`, and
`BLOCKED_BY_OTHER_TICKET`.

<!-- P10_BACKLOG_START -->
| ID | Cluster | Status | depends_on | decision_required | target_artifact | production change allowed? |
|---|---|---|---|---|---|---|
| T1-B | A Open Safety | NEEDS_PRODUCT | P10-D1 | duplicate scope, scale-in, cross-system/side policy, exchange acknowledgement | approved open idempotency contract + race/crash acceptance matrix | NO |
| T5 | A Open Safety | NEEDS_PRODUCT | P10-D2; identity relation from P10-D1/D5 | fill-to-protection SLO, whether open requires protection, failure response | protection establishment model + idle/backlog latency characterization | NO |
| PMB-23A | B Close/Reconcile Durability | NEEDS_PRODUCT | P10-D3/D4/D5 | duplicate-versus-loss preference and durable recorder sink | ghost-close durability/replay contract | NO |
| T12 | B Close/Reconcile Durability | NEEDS_DESIGN | P10-D1/D2/D3/D4/D5 | authority and recovery guarantees for open/close/save/restart | T12-A/B/C/D crash-consistency design | NO |
| PMB-26C1 | B Close/Reconcile Durability | NEEDS_PRODUCT | P10-D4 | required PG durability versus explicit best-effort audit | normalized PG failure policy | NO |
| PMB-26C2 | B Close/Reconcile Durability | BLOCKED_BY_OTHER_TICKET | PMB-26C1; PMB-26B2; T12 | rollback/retry/batch policy after required persistence failure | staged partial-commit and retry model | NO |
| PMB-24 | D Product Policy | NEEDS_PRODUCT | product decision; implementation later P10-D3/D5 | internal repair, business close, or discrepancy quarantine | reconcile divergence policy | NO |
| PMB-26B2 | D Product Policy | NEEDS_PRODUCT | product decision; implementation later P10-D4/C2 | at-most-once, at-least-once, bounded retry, or escalation | notification acknowledgement/retry policy | NO |
| POS-ID | C Identity/Marker Model | NEEDS_DESIGN | P10-D5 | canonical episode/request/order/fill identity and migration | identity schema, alias map, migration contract | NO |
| PMB-4 | C Identity/Marker Model | BLOCKED_BY_OTHER_TICKET | POS-ID; P10-D5 | lease versus tombstone/cooldown scope, expiry, CAS cleanup | marker authority/schema/migration model | NO |
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
| P10-D1 Open Idempotency Model | proposed | active path + request identity + broker acknowledgement contract | T1-B, T12-A, T5 identity |
| P10-D2 Protection Establishment Model | proposed | protection SLO + desired state + worker/retry/restart contract | T5, T12-A |
| P10-D3 Crash Consistency Model | proposed | lifecycle saga/stages + compensation/recovery | T12, PMB-23A, C2, PMB-24 implementation |
| P10-D4 Persistence Failure Policy | proposed | PG/recorder/TG acknowledgement and retry contract | C1, C2, B2 implementation |
| P10-D5 Position Identity And Marker Authority | proposed | canonical aliases + migration + lease/tombstone schema | POS-ID, PMB-4, replay identity |

## Recommended Order

1. P10-D1 as P10-01.
2. P10-D2 product SLO and timing characterization in parallel.
3. P10-D5 identity and marker authority.
4. P10-D4 persistence and delivery policy.
5. P10-D3 integrated crash/restart model.
6. Behavior tickets only after their predecessor artifacts are approved.

No imported backlog ticket is implementation-ready at P10-00.
