# Phase 10 Reliability Backlog

> Imported exactly from `PHASE9_CLOSURE.md`. P10-00 is architecture-only;
> production change is not allowed for any row below.

## Status Vocabulary

`READY`, `READY_FOR_DESIGN`, `READY_FOR_IMPLEMENTATION`, `NEEDS_DESIGN`,
`NEEDS_PRODUCT`, `NEEDS_CHARACTERIZATION`, `BLOCKED_BY_OTHER_TICKET`,
`BLOCKED_BY_PRODUCT_DECISION`, `BLOCKED_BY_SPECIFIC_PRODUCT_DECISION`,
`BLOCKED_BY_D5A`, `READY_AFTER_D5A_FOUNDATION`, `BLOCKED_BY_D3`, and
`DESIGN AUDITED`, `IMPLEMENTED / CLOSED`.
`SUPERSEDED_BY_D3_DESIGN` means the design scope was consolidated into P10-D3;
it does not mean the production behavior was implemented.

<!-- P10_BACKLOG_START -->
| ID | Cluster | Status | depends_on | decision_required | target_artifact | production change allowed? |
|---|---|---|---|---|---|---|
| T1-B | A Open Safety | BLOCKED_BY_PRODUCT_DECISION | P10-D1 DESIGN AUDITED | duplicate scope, scale-in, cross-system/side policy, UNKNOWN timeout policy, retention and restart guarantee | approved product contract, then D1A-D1D implementation tickets | NO |
| T5 | A Open Safety | BLOCKED_BY_PRODUCT_DECISION | P10-D2 DESIGN AUDITED; implementation identity from P10-D5 | verified fill-to-protection SLO, open/admission semantics, failure action, replacement gap/overlap, restart guarantee | approved product contract, then D2A-D2E implementation tickets | NO |
| PMB-23A | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION | P10-D4/D5 DESIGN AUDITED; implementation P10-D3 | duplicate-versus-loss preference and authoritative close recorder sink | approved durability contract, then D3 ghost-close replay design | NO |
| T12 | B Close/Reconcile Durability | SUPERSEDED_BY_D3_DESIGN | P10-D1/D2/D3/D4/D5 DESIGN AUDITED | implementation decisions for authority, acknowledgement, compensation, and restart guarantees | P10-D3A-D3E implementation tickets after named gates | NO |
| PMB-26C1 | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION | P10-D4 DESIGN AUDITED | PG event ledger required durability versus explicit best-effort audit | approved event durability contract, then D4B/D3 | NO |
| PMB-26C2 | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION | PMB-26C1; PMB-26B2; P10-D3 DESIGN AUDITED; D3A-D3D implementation | partial-commit isolation, retry, and compensation after selected C1/B2 policy | approved C1/B2 policy, then D3 staged replay implementation | NO |
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
| P10-D3 Crash Consistency Model | **DESIGN AUDITED** | `P10_D3_CRASH_CONSISTENCY_MODEL.md`; operation state/commit + UNKNOWN/replay/CAS/fencing/compensation using D1/D2/D4/D5 | supersedes T12 design; defines D3A-D3E gates for PMB-23A, C2, and PMB-24 implementation |
| P10-D3D Generation Fencing Audit | **DESIGN AUDITED** | `P10_D3D_GENERATION_FENCING_AUDIT.md`; stale-work taxonomy + protection ABA golden + minimal queue fence/readiness | D3D-1 first implementation boundary; D3D-2/D3D-3/D3D-4 remain separate |
| P10-D3D-1 Episode Fence Authority | **DESIGN AUDITED** | `P10_D3D1_EPISODE_FENCE_AUTHORITY.md`; normalized slot + opaque episode ID + slot generation + legacy/reconstructed authority | narrows D3D-1 to BLOCKED_BY_D5A and defines D3D-1A-E order |
| P10-D5A Episode Authority Readiness | **DESIGN AUDITED** | `P10_D5A_EPISODE_AUTHORITY_READINESS.md`; principal + legacy adoption/quarantine + reconstructed quarantine + Redis CAS readiness | makes D5A staged foundation ready and D3D-1 ready after it |
| P10-D5A-1 Slot Namespace | **IMPLEMENTED / CLOSED** | `position_identity`; `P10_D5A1_SLOT_NAMESPACE_IMPLEMENTATION.md`; pure principal/slot value objects with no runtime wiring | P10-07C D5A-2 authority store |
| P10-D5A-2 Slot Authority Store | **IMPLEMENTED / CLOSED** | `position_identity.authority*`; `P10_D5A2_SLOT_AUTHORITY_IMPLEMENTATION.md`; dormant record + Lua CAS + generation high-water | P10-07D D5A-3 adoption/reconstruction foundation |
| P10-D5A-3 Episode Onboarding | **IMPLEMENTED / CLOSED** | `position_identity.adoption`; `P10_D5A3_EPISODE_ONBOARDING_IMPLEMENTATION.md`; dormant controlled adoption + reconstructed quarantine | completes D5A foundation |
| P10-D3D-1B Queue Identity | **IMPLEMENTED / CLOSED** | `position_protection.task`; `P10_D3D1B_QUEUE_IDENTITY_IMPLEMENTATION.md`; immutable payload + legacy drop parser | P10-D3D-1C worker preflight |
| P10-D3D-1C Worker Preflight | **IMPLEMENTED / CLOSED** | `position_protection.fence*`; `P10_D3D1C_WORKER_PREFLIGHT_IMPLEMENTATION.md`; V1/V2 + Redis mutation claim | P10-D3D-1D conditional writeback |
| R1 Canonical Live Projection | **IMPLEMENTED / CLOSED** | `position_identity.projection*`; `P10_R1_CANONICAL_LIVE_PROJECTION_IMPLEMENTATION.md`; dormant per-slot schema/CAS + legacy compatibility guard | R2 / P10-D2A desired-protection authority |
| R2 / P10-D2A Desired Protection | **IMPLEMENTED / CLOSED** | `position_protection.desired*`; `P10_R2_DESIRED_PROTECTION_IMPLEMENTATION.md`; dormant desired generation/state + Redis CAS | R3 / P10-D3D-1D conditional alias writeback |
| R3 / P10-D3D-1D Conditional Writeback | **IMPLEMENTED / CLOSED** | `position_protection.writeback*`; `P10_R3_CONDITIONAL_ALIAS_WRITEBACK_IMPLEMENTATION.md`; atomic claim/desired/projection ACK writeback | R4 active producer wiring |
| R4 Active Producer Wiring | IN_PROGRESS | native/migrated/replacement producer identity and V3 enqueue wiring | R4A native open closed; remaining producers pending |
| R4A Native Active Open | **IMPLEMENTED / CLOSED** | `position_protection.handoff*`; `P10_R4A_NATIVE_OPEN_PRODUCER_IMPLEMENTATION.md`; atomic three-record handoff + configured active-open V3 enqueue | R4A-CLOSE closed; proceed to controlled migrated producer |
| R4A-CLOSE Canonical Close Finalization | **IMPLEMENTED / CLOSED** | `position_identity.close_finalizer`; `P10_R4A_CLOSE_FINALIZATION_IMPLEMENTATION.md`; generation-fenced ACTIVE-to-FLAT CAS after exchange-flat evidence | R4B controlled migrated producer |
| R4B-IDENTITY Legacy Identity Handoff | **IMPLEMENTED / CLOSED (DORMANT)** | `position_identity.migration_handoff`; `P10_R4B_IDENTITY_HANDOFF_IMPLEMENTATION.md`; atomic MIGRATED authority + projection CAS | writer guard + protection verification before active R4B producer |
| R4B-WRITER Legacy Snapshot Fence | **IMPLEMENTED / CLOSED (DORMANT)** | `position_identity.snapshot_fence`; `P10_R4B_WRITER_FENCE_IMPLEMENTATION.md`; per-symbol batch filtering + fail-closed canonical read validation | typed save ACK + canonical mutation CAS before runtime wiring |
| R4B-MUTATION Projection Reduction CAS | **IMPLEMENTED / CLOSED (DORMANT)** | `position_identity.projection_mutation`; `P10_R4B_PROJECTION_MUTATION_IMPLEMENTATION.md`; authority+projection fenced strict quantity reduction | lifecycle wiring + typed save ACK |
| R4B-STATE-ACK Typed Snapshot CAS | **IMPLEMENTED / CLOSED (DORMANT)** | `position_state.strict_snapshot`; `P10_R4B_TYPED_SNAPSHOT_IMPLEMENTATION.md`; strict raw-token CAS + typed ACK/STALE/UNKNOWN | compose with canonical fence + propagate caller ACK |
| R4B-COMPOSE Atomic Fenced Snapshot Commit | **IMPLEMENTED / CLOSED (DORMANT)** | `position_state.fenced_snapshot`; `P10_R4B_FENCED_SNAPSHOT_COMMIT_IMPLEMENTATION.md`; one Lua fence over snapshot + every canonical raw token | propagate caller ACK + lifecycle wiring + protection verification |
| R4B-ACK-POLICY Typed Snapshot Caller Decisions | **IMPLEMENTED / CLOSED (DORMANT)** | `position_state.snapshot_ack`; `P10_R4B_SNAPSHOT_ACK_POLICY_IMPLEMENTATION.md`; exhaustive result-to-action policy + non-swallowing service | runtime ACK propagation + lifecycle wiring + protection verification |
| R4B-ACK-PROPAGATION Lifecycle Continuation Boundary | **IMPLEMENTED / CLOSED (DORMANT)** | `position_state.lifecycle_ack`; `P10_R4B_ACK_PROPAGATION_IMPLEMENTATION.md`; commit-once and continue-only-after-ACK boundary | select durable lifecycle caller + blocked-outcome recovery |
| R4B-RECOVERY-HANDOFF Blocked Outcome Durable Port | **IMPLEMENTED / CLOSED (DORMANT)** | `position_state.recovery_handoff`; `P10_R4B_RECOVERY_HANDOFF_IMPLEMENTATION.md`; exact blocked operation + typed durable-sink ACK seam | approve D3A backend/schema/replay contract |
| R4B-MIGRATED-PROTECTION-PLAN | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.migrated_plan`; acknowledged handoff to exact PENDING desired protection | durable declare + exchange verification |
| R4B-MIGRATED-DESIRED-DECLARATION | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.migrated_declaration`; declare-once + exact durable record ACK | compose signed exchange verification |
| R4B-MIGRATED-VERIFICATION-COMPOSITION | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.migrated_verification`; exact declaration gate + one coordinator step | durable scheduling + default-off runtime wiring |
| R4B-PROTECTION-VERIFY Strict Exchange Evidence Core | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.verification`; `P10_R4B_PROTECTION_VERIFICATION_IMPLEMENTATION.md`; exact identity + fresh exposure + unique active reduce-only STOP proof | Binance payload adapter + atomic ACTIVE commit + runtime wiring |
| R4B-PROTECTION-COMMIT Verified ACTIVE CAS | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.verification_commit`; `P10_R4B_VERIFIED_ACTIVE_COMMIT_IMPLEMENTATION.md`; verified evidence + three-token Lua CAS + UNKNOWN recovery | Binance normalization/reverification + runtime wiring |
| D2C Binance Verification Adapter | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.binance_observation` + `verification_retry`; `P10_D2C_BINANCE_VERIFICATION_ADAPTER_IMPLEMENTATION.md`; strict new Algo schema + bounded attempt/deadline policy | signed transport characterization + runtime wiring |
| D2C Verification Coordinator | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.verification_coordinator`; `P10_D2C_VERIFICATION_COORDINATOR_IMPLEMENTATION.md`; one query + normalize + commit step with typed scheduling actions | signed transport + durable scheduling + runtime wiring |
| D2C Signed Query Transport | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.binance_query`; `P10_D2C_SIGNED_QUERY_TRANSPORT_IMPLEMENTATION.md`; explicit endpoint config + two symbol-scoped injected signed GETs + completion clock | endpoint approval + default-off runtime wiring |
| D2C Runtime Activation Gate | **IMPLEMENTED / CLOSED (DORMANT)** | `position_protection.verification_activation`; `P10_D2C_RUNTIME_ACTIVATION_GATE_IMPLEMENTATION.md`; default-off fail-closed endpoint/scheduler/operator readiness | approve endpoint + durable backend + operator runbook before wiring |
| P10-D4 Persistence Failure Policy | **DESIGN AUDITED** | `P10_D4_PERSISTENCE_FAILURE_POLICY.md`; sink roles + acknowledgement + required/best-effort/retry/UNKNOWN + D3 input contract | C1, C2, PMB-23A, B2, T12-C design |
| P10-D5 Position Identity And Marker Authority | **DESIGN AUDITED** | `P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md`; identity taxonomy + episode/reopen semantics + marker authority/migration/fencing model | POS-ID, PMB-4, D2 generation, T12/D3 replay identity |

## Recommended Order

1. P10-D1 as P10-01.
2. P10-D2 product SLO and timing characterization in parallel.
3. P10-D5 identity and marker authority.
4. P10-D4 persistence and delivery policy.
5. P10-D3 integrated crash/restart model (design audited as P10-05).
6. P10-D3D stale async-work fencing audit (design audited as P10-06A).
7. P10-D3D-1 episode fence authority contract (design audited as P10-06B).
8. P10-D5A authority readiness (design audited as P10-07).
9. P10-07B D5A-1 slot namespace and principal resolver (implemented/closed).
10. P10-07C D5A-2 dedicated slot authority store and generation CAS (implemented/closed).
11. P10-07D D5A-3 controlled adoption and reconstructed quarantine foundation (implemented/closed).
12. P10-D3D-1B immutable queue identity extension and legacy-item drop (implemented/closed).
13. P10-D3D-1C worker validation/mutation claim after D3D-1B (implemented/closed).
14. R1 canonical live projection/revision and dormant legacy guard (implemented/closed).
15. R2 / P10-D2A durable desired-protection record after R1 (implemented/closed).
16. R3 conditional writeback (implemented/closed).
17. R4A native active-open and R4A-CLOSE finalization (implemented/closed); R4B dormant identity, writer, mutation, state-ACK, and atomic composition nodes implemented, then complete caller ACK propagation, lifecycle wiring, and protection verification before active R4B producer.
18. Remaining behavior tickets only after their predecessor artifacts are approved.

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
| P10-D2A | durable desired-protection episode/generation record | IMPLEMENTED / CLOSED through R2 | YES; dormant foundation only |
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
| P10-D5A | normalized slot, immutable episode authority, slot generation, provenance, CAS, controlled adoption, and reconstructed quarantine | IMPLEMENTED / CLOSED through P10-07B/C/D | YES; dormant foundation only, no active-flow wiring |
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
`BLOCKED_BY_PRODUCT_DECISION`. P10-D4 and P10-D3 are audited below.
See `docs/v2/P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md`.

## P10-D4 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D4A | Redis/file operational-state acknowledgement and revision contract | BLOCKED_BY_PRODUCT_DECISION_AND_D3 | NO |
| P10-D4B | PG event required-versus-best-effort audit contract | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4C | authoritative close/partial accounting and finalization ACK | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4D | ClickHouse loss policy and notification delivery acknowledgement | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D4E | durable operation journal stage/backend/replay model | BLOCKED_BY_PRODUCT_DECISION; design supplied by D3A-D3C | NO |

## P10-D4 Readiness

| Ticket | Readiness |
|---|---|
| PMB-23A | BLOCKED_BY_PRODUCT_DECISION; implementation also needs D3 |
| PMB-26C1 | BLOCKED_BY_PRODUCT_DECISION |
| PMB-26C2 | BLOCKED_BY_PRODUCT_DECISION for C1/B2; implementation also needs D3A-D3D |
| T12-C | SUPERSEDED_BY_D3_DESIGN; implementation maps to D3A-D3D |
| P10-D3 | DESIGN AUDITED as P10-05 |

P10-D4 is design-complete. No persistence-policy behavior ticket is ready for
implementation. See `docs/v2/P10_D4_PERSISTENCE_FAILURE_POLICY.md`.

## P10-D3 Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D3A | operation journal authority/schema, CAS, leases, and retention | BLOCKED_BY_PRODUCT_DECISION | NO |
| P10-D3B | startup/continuous recovery and idempotent stage replay | BLOCKED_BY_D3A_D3C_D3D | NO |
| P10-D3C | exchange UNKNOWN resolver for orders, fills, positions, and algo orders | BLOCKED_BY_D1_D2_D4_D5_AND_PRODUCT_DECISION | NO |
| P10-D3D | generation-fencing family; protection queue, marker, and reconcile splits | DESIGN AUDITED as P10-06A; see D3D-1 through D3D-4 | NO |
| P10-D3E | structured result and legacy bool/None compatibility adapter | BLOCKED_BY_PRODUCT_DECISION | NO |

## P10-D3 Readiness

| Ticket | Readiness |
|---|---|
| T12 | SUPERSEDED_BY_D3_DESIGN; runtime behavior remains unimplemented |
| PMB-23A | BLOCKED_BY_PRODUCT_DECISION; implementation also needs D3A-D3D |
| PMB-26C2 | BLOCKED_BY_PRODUCT_DECISION; implementation also needs D3A-D3D |
| PMB-24 | NEEDS_PRODUCT before reconcile behavior implementation |
| P10-D3A-D3E | none ready for production implementation |

P10-D3 is design-complete. It defines durable intent, commit points, UNKNOWN
resolution, restart replay, CAS/lease ownership, generation fencing,
compensation, and compatibility requirements. No crash-consistency behavior
ticket is implementation-ready. See
`docs/v2/P10_D3_CRASH_CONSISTENCY_MODEL.md`.

## P10-D3D Suggested Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-D3D-1 | protection queue episode fence before cancel/create/writeback | READY_AFTER_D5A_FOUNDATION | NO |
| P10-D3D-2 | monotonic desired-protection generation fence within one episode | BLOCKED_BY_D2_PRODUCT_DECISION | NO |
| P10-D3D-3 | episode/operation-scoped marker compare-and-clear | BLOCKED_BY_D5_PRODUCT_DECISION | NO |
| P10-D3D-4 | reconcile/monitor projection revision and generation CAS | BLOCKED_BY_D5_PRODUCT_DECISION_AND_D4_ACK_CONTRACT | NO |

## P10-D3D Readiness

D3D-1 is the smallest recommended behavior scope and cannot use current
`position_id` as authority. P10-07 resolves D5A inputs and selects a staged
foundation: explicit non-secret principal, dedicated Redis Lua CAS authority,
controlled legacy adoption with quarantine fallback, and automatic reconstructed
authority only as quarantined. Scale-in, cross-system allocation, full history
migration, marker redesign, journal, and replay are not D3D-1 blockers.

The selected future fail-safe is to drop missing, malformed, mismatched, or
legacy-unfenced tasks before any exchange mutation, then let D2 health/emergency
policy handle an unprotected current episode. Normal process-restart deployment
naturally clears old four-tuples because the queue is memory-only; this is not
restart recovery.

P10-D3D is design-complete. No generation field, queue payload, marker, Redis
schema, journal, replay, or production behavior changed. See
`docs/v2/P10_D3D_GENERATION_FENCING_AUDIT.md`.

## P10-D3D-1 Authority And Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| D5A / P10-D3D-1A | exchange-position-key namespace, episode field, slot generation, provenance, CAS | IMPLEMENTED / CLOSED through P10-07B/C/D | YES; dormant foundation only |
| P10-D3D-1B | immutable queue identity extension and four-tuple legacy drop parser | IMPLEMENTED / CLOSED | NO; isolated V2 must not deploy alone |
| P10-D3D-1C | worker V1/V2 validation and mutation claim before cancel/create | IMPLEMENTED / CLOSED | NO; isolated V2 must not deploy alone |
| P10-D3D-1D | episode/generation/revision conditional `algo_sl_id` writeback | IMPLEMENTED / CLOSED through R3 | NO; isolated V2 only |
| P10-D3D-1E | controlled legacy adoption/quarantine and reconstructed provenance guard | IMPLEMENTED / CLOSED through P10-07D / D5A-3 | YES; dormant foundation only |

P10-07 resolves the former D5A decisions: explicit configured principal;
controlled adoption when strict evidence/CAS passes and quarantine otherwise;
automatic reconstructed authority only as `RECONSTRUCTED_QUARANTINED`.
D5A foundation and D3D-1B/C are complete. R1 canonical live-projection revision/CAS is implemented and closed as dormant infrastructure. R2 durable desired-protection generation is implemented and closed as dormant infrastructure. R3 / D3D-1D conditional writeback is implemented and closed on isolated V2; R4A native active-open and R4A-CLOSE finalization are implemented; R4B atomic snapshot fence/CAS composition is implemented and dormant, while migrated and replacement producers remain.

P10-D3D-1 is design-complete; implementation proceeds through the remaining R4 producers after R4A. Current
`position_id` is `TEMPORARY_FENCE_ONLY`, never canonical authority. See
`docs/v2/P10_D3D1_EPISODE_FENCE_AUTHORITY.md`.

## P10-D5A Foundation Implementation Split

| ID | Scope | Readiness | Production allowed now? |
|---|---|---|---|
| P10-07B / D5A-1 | non-secret principal resolver, PROD/DEMO/SANDBOX + ONE_WAY/BOTH normalization, canonical slot-key value object | IMPLEMENTED / CLOSED | YES; dormant behavior-zero leaf module |
| P10-07C / D5A-2 | dedicated slot authority adapter, Redis Lua CAS, generation high-water, typed acknowledgement | IMPLEMENTED / CLOSED | YES; dormant authority adapter |
| P10-07D / D5A-3 | controlled legacy adoption and reconstructed-quarantine foundation | IMPLEMENTED / CLOSED | YES; dormant orchestration with no active-flow wiring |

P10-07B, P10-07C, and P10-07D implemented the dormant D5A foundation without
wiring active open, close, position state, queue, worker, monitor, reconcile,
marker, or startup behavior.

P10-D5A, P10-D3D-1B/C, R1, and R2 are `IMPLEMENTED / CLOSED`; P10-D3D-1D is `IMPLEMENTED / CLOSED` through R3; R4 is `IN_PROGRESS`, with R4A native active open and R4A-CLOSE finalization implemented/closed. The execution order is maintained in `docs/v2/V2_UPGRADE_MASTER_PLAN.md`.
See
`docs/v2/P10_D5A_EPISODE_AUTHORITY_READINESS.md`.
