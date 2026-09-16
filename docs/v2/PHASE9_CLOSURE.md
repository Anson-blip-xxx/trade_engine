# Phase 9 Closure - Behavior Stabilization

## 1. Phase Goal And Decision

Phase 9 converted inherited behavior observations into bounded outcomes:

- deterministic defects received minimal production fixes;
- intentional behavior received characterization without production changes;
- questions not answerable from code received explicit product or design owners;
- every production fix remains independently reversible.

Audit range: base `1f9eb00` through pre-closure HEAD `636ed48`.

**Closure decision: PHASE 9 READY TO CLOSE.**

No high-risk ticket remains `OPEN / READY`. Remaining risks require product
policy or cross-boundary design and are explicitly deferred rather than treated
as implicit blockers.

## 2. Status Vocabulary

Only these terminal audit statuses are used:

- `FIXED / CLOSED`
- `REVIEWED / INTENTIONAL / CLOSED`
- `DEFERRED / NEEDS_PRODUCT_DECISION`
- `DEFERRED / DESIGN_REQUIRED`
- `OPEN / READY`
- `OPEN / CHARACTERIZATION_NEEDED`
- `NO-RISK / KEEP`
- `INVALID / SUPERSEDED`

## 3. Final Ticket Inventory

The parent rows preserve the original P9-00 inventory. Split rows are the
authoritative executable tickets.

<!-- FINAL_STATUS_START -->
| Ticket | Status | Original risk | Characterization | Fix | Current production behavior | Rollback | Residual risk | Next owner |
|---|---|---|---|---|---|---|---|---|
| S0-1 | FIXED / CLOSED | S0 produced `market_state`; executor read `market_mode`, so risk-off failed open | `f50ec79` | `8ee0ea5` | `market_state` is authoritative; legacy key/value fallback remains | `git revert 8ee0ea5` | Unknown/missing state still fails open by existing policy | Strategy/Execution |
| PMB-9 | FIXED / CLOSED | fallback AlgoSL cancel used GET | `3b25d77` | `f1d3cd2` | fallback cancel slot uses `_light_fapi_delete` | `git revert f1d3cd2` | cancellation remains best-effort | Protection |
| PMB-17 | FIXED / CLOSED | malformed ghost tuple bypassed side filter or crashed | `71327c2` | `32412d9` | malformed entries are logged and dropped | `git revert 32412d9` | process-local queue and unknown non-empty side values remain | Monitoring |
| PMB-23 | INVALID / SUPERSEDED | pop-before-record and whole-batch abort were combined | `65a9da5` | n/a | split into PMB-23A and PMB-23B | n/a | see child tickets | Phase owner |
| PMB-23A | DEFERRED / NEEDS_PRODUCT_DECISION | record failure after pop can persist local removal without ledger/marker | inherited goldens + `65a9da5` | n/a | `pop -> record -> mark` remains | n/a | choosing record-first may create replay/duplicate multi-sink writes | Product + Reconcile/Ledger |
| PMB-23B | FIXED / CLOSED | one symbol failure skipped later ghost symbols | `65a9da5` | `11d5283` | symbol-local failure logs and continues; global fetch still aborts | `git revert 11d5283` | PMB-23A remains independent | Reconcile |
| PMB-24 | DEFERRED / NEEDS_PRODUCT_DECISION | `reconcile_all` silently removes local-only state without record/marker/lock | inherited reconcile goldens | n/a | silent reconcile channel remains separate from ghost cleanup | n/a | state/ledger divergence and policy duplication | Product + Reconcile/Ledger |
| PMB-26 | INVALID / SUPERSEDED | marker, TG, and PG semantics were one ambiguous ticket | P9-07A through P9-09A | n/a | split into A, B, B2, C1, and C2 | n/a | see child tickets | Phase owner |
| PMB-26A | REVIEWED / INTENTIONAL / CLOSED | fresh marker is cleared when exchange still has a position | `c92b849` | n/a | exchange reality wins; marker self-heals before notification | n/a | expired/malformed residue belongs to PMB-4 | Reconcile |
| PMB-26B | REVIEWED / INTENTIONAL / CLOSED | Telegram exception is swallowed | `2b37d84` | n/a | TG remains best-effort and does not block PG/reconcile | n/a | delivery acknowledgement belongs to PMB-26B2 | Notification |
| PMB-26B2 | DEFERRED / NEEDS_PRODUCT_DECISION | failed TG attempt is still suppressed by seen for 24 hours | `2b37d84` | n/a | seen means attempted, not acknowledged | n/a | missed alert without immediate retry | Product + Notification |
| PMB-26C | INVALID / SUPERSEDED | PG `False` and raised exceptions have different topology | `636ed48` | n/a | policy and partial-commit concerns split into C1/C2 | n/a | see child tickets | Phase owner |
| PMB-26C1 | DEFERRED / NEEDS_PRODUCT_DECISION | no authoritative PG failure policy | `636ed48` | n/a | DB errors usually become ignored `False`; pre-try/callback errors can raise | n/a | silent audit loss or fail-fast monitor interruption | Product + Ledger |
| PMB-26C2 | DEFERRED / NEEDS_PRODUCT_DECISION | raised PG error occurs after marker/alert/TG but before merge save | `636ed48` | n/a | partial side effects survive; later symbols and snapshot save are skipped | n/a | partial commit, starvation, and 24-hour retry suppression | Product + Reconcile/State/Ledger |
| PMB-27 | FIXED / CLOSED | non-final close left marker and blocked retry | `994a8ca` | `eb6c14f` | no-fill/partial branches clear marker before returning false | `git revert eb6c14f` | marker-first model otherwise remains | Lifecycle |
| PMB-29 | NO-RISK / KEEP | `_set_cooldown` is a noop | inherited lifecycle goldens | n/a | compatibility seam has no production caller; strategy/ledger own cooldown | n/a | implementing it could overwrite concurrent strategy state | Lifecycle |
| PMB-30 | FIXED / CLOSED | duplicate order could enqueue an orphan AlgoSL before duplicate check | `2b866b2` | `e4a4a2a` | duplicate-ordering branch is eliminated by T1-A | `git revert e4a4a2a` | concurrent/save-failure paths are re-owned by T1-B/T12 | Lifecycle |
| T1 | INVALID / SUPERSEDED | duplicate-open idempotency combined sequential and concurrent cases | `2b866b2` | n/a | split into T1-A and T1-B | n/a | see child tickets | Phase owner |
| T1-A | FIXED / CLOSED | sequential local duplicate sent order before checking state | `2b866b2` | `e4a4a2a` | symbol precheck occurs before exchange and AlgoSL side effects | `git revert e4a4a2a` | no concurrent or save-failure idempotency | Lifecycle |
| T1-B | DEFERRED / NEEDS_PRODUCT_DECISION | concurrent opens can both pass local precheck | `2b866b2` | n/a | no open-scoped distributed lock or broker idempotency key | n/a | duplicate real orders and protection tasks | Product + Lifecycle/Execution |
| T5 | DEFERRED / DESIGN_REQUIRED | asynchronous protection queue has polling/backlog delay and no retry | existing protection worker/failure goldens | n/a | queue remains in-memory; placement is paced by 11-second sleep | n/a | unprotected exposure, restart loss, backlog amplification | Product + Protection/Execution |
| T12 | DEFERRED / DESIGN_REQUIRED | save/record/PG ordering has crash-consistency gaps | lifecycle ordering goldens | n/a | branch-specific ordering remains intentionally unequal | n/a | state, exchange, and ledger can diverge after crashes | Product + State/Lifecycle/Ledger |
| T14 | INVALID / SUPERSEDED | duplicate check needed to move before order and AlgoSL enqueue | `2b866b2` | `e4a4a2a` | original work is fully covered by T1-A | `git revert e4a4a2a` | broader concurrency belongs to T1-B | Phase owner |
| POS-ID | DEFERRED / DESIGN_REQUIRED | aggregate position IDs have multiple formats and creation points | inherited state/ledger goldens | n/a | explicit IDs are preserved; fallback formats remain non-canonical | n/a | joins, UPSERT, partial aggregation, and migration continuity | State/Ledger architecture |
| PMB-4 | DEFERRED / DESIGN_REQUIRED | expired/malformed closed markers remain without Redis TTL | inherited marker goldens + `c92b849` | n/a | timestamp window fails open; stale keys remain until explicit clear | n/a | stale-key growth and malformed-marker dedup loss | State/Redis architecture |
<!-- FINAL_STATUS_END -->

There are no `OPEN / READY`, `OPEN / CHARACTERIZATION_NEEDED`, or UNKNOWN
tickets at closure.

## 4. Fixed Tickets And Golden Coverage

| Ticket | Reproduction/characterization | Fixed regression evidence | Scoped result |
|---|---|---|---|
| S0-1 | `test_phase9_s0_reader_characterization.py` | producer-shaped risk-off and authority matrix | risk gate key/vocabulary aligned |
| PMB-9 | `test_phase9_pmb9_cancel_fallback_characterization.py` | fallback identity and DELETE request | fallback cancel corrected |
| T1-A / PMB-30 | `test_phase9_pmb9_idempotency_audit.py` | zero order/enqueue/save effects on duplicate | sequential duplicate ordering corrected |
| PMB-27 | `test_phase9_pmb27_partial_marker_characterization.py` | no-fill/partial marker clear and retry entry | non-final retry unblocked |
| PMB-23B | `test_phase9_pmb23b_batch_isolation_characterization.py` | A/B/C continuation and lock release | symbol failures isolated |
| PMB-17 | `test_phase9_pmb17_ghost_tuple_characterization.py` | malformed shape matrix and valid routing | malformed entries cannot cross-consume/crash |

Every fixed ticket has a pre-fix reproduction, a post-fix regression assertion,
an isolated production commit, and a single-commit rollback.

## 5. Intentional Tickets

| Ticket | Proven intent | Evidence |
|---|---|---|
| PMB-26A | exchange position is more authoritative than a fresh local closed marker | `P9_PMB26A_GOLDEN.md`, `c92b849` |
| PMB-26B | Telegram is best-effort and must not block PG/reconcile | `P9_PMB26B_GOLDEN.md`, `2b37d84` |

Neither intentional ticket changed production code.

## 6. Deferred Decision Questions

| Ticket | Required answer before implementation |
|---|---|
| T1-B | Should concurrent idempotency use a per-symbol lock, broker `clientOrderId`, exchange-position precheck, or a combination; what is the cross-system/side scope? |
| T5 | What maximum fill-to-protection latency is acceptable, and should initial SL placement be synchronous, prioritized, rate-limited, durable, or retried? |
| T12 | Which state/ledger ordering is authoritative, and what recovery or compensation guarantee is required after a process crash? |
| PMB-23A | Is missing a close record preferable to duplicate/replayed PG, CH, analysis, and notification effects; which sink defines durable success? |
| PMB-24 | Is `reconcile_all` diagnostic state repair, or must it use ghost cleanup's lock, record, marker, and tradable policy? |
| PMB-26B2 | Does seen mean attempted, Telegram-acknowledged, or durably recorded; what retry/backoff and duplicate-alert policy is acceptable? |
| PMB-26C1 | Must PG `False` and exceptions fail reconciliation, or should PG be explicitly best-effort with logging/metrics? |
| PMB-26C2 | Which marker, alert, TG, merge, save, and remaining-batch effects must roll back or retry after failed durable persistence? |

## 7. Design-Deferred Questions

| Ticket | Design work required |
|---|---|
| POS-ID | Define canonical schema and creation point, stability under weighted entry changes, and migration/aliasing for persisted IDs and historical ledger rows. |
| PMB-4 | Preserve variable timestamp windows while defining schema validation, stale-key cleanup, and race-safe compare-and-delete across PM and executor readers. |
| T5 | Protection architecture must meet the product latency SLO without violating exchange pacing or introducing duplicate SL orders. |
| T12 | A transaction/outbox/recovery design must span exchange reality, Redis state, event ledger, and episode recorder. |

## 8. Risk Ledger

| Ticket/cluster | Risk before | Result | Residual risk |
|---|---|---|---|
| S0-1 | wrong risk-off gate | FIXED | fail-open for unknown/missing state remains scoped behavior |
| PMB-9 | cancel fallback did not delete | FIXED | transport remains best-effort |
| T1-A / PMB-30 | sequential duplicate real order and orphan SL | FIXED | concurrency/save-failure moved to T1-B/T12 |
| PMB-27 | partial/no-fill marker blocked close retry | FIXED | marker-first model remains |
| PMB-23B | one failure aborted later symbols | FIXED | pop-before-record remains PMB-23A |
| PMB-17 | malformed queue entry bypassed routing/crashed | FIXED | malformed data is dropped, not recovered |
| PMB-26A | marker self-heal looked unsafe | KEEP | expired/malformed residue is PMB-4 |
| PMB-26B | TG exception swallow looked inconsistent | KEEP | delivery acknowledgement is PMB-26B2 |
| PMB-26C | PG failure policy/atomicity conflict | DEFERRED | C1/C2 require product policy |
| PMB-23A / PMB-24 | record/state consistency gaps | DEFERRED | explicit product semantics required |
| T5 / T12 | protection and crash-consistency design risk | DEFERRED | architecture work required |
| POS-ID / PMB-4 | identity and marker normalization | DEFERRED | migration/race-safe design required |
| PMB-29 | unused noop seam | NO-RISK | do not activate without a new ticket |
| T14 | duplicate-ordering alias | SUPERSEDED | T1-B owns remaining concurrency scope |

## 9. Production Behavior Commit Ledger

Only these six commits change runtime behavior:

| Commit | Ticket | Production file | Minimal delta |
|---|---|---|---|
| `8ee0ea5` | S0-1 | `strategies/shared_executor.py` | align state key/value gate |
| `f1d3cd2` | PMB-9 | `shared/position_manager.py` | fallback tuple points to DELETE helper |
| `e4a4a2a` | T1-A / PMB-30 | `position_lifecycle/service.py` | move local duplicate precheck before side effects |
| `eb6c14f` | PMB-27 | `position_lifecycle/service.py` | clear marker on two non-final branches |
| `11d5283` | PMB-23B | `position_reconcile/service.py` | add symbol-local exception boundary |
| `32412d9` | PMB-17 | `position_monitoring/service.py` | validate malformed queue shape before routing |

Across Phase 9, production changes are limited to five files. There is no
architecture migration, unrelated refactor, bundled policy change, retry queue,
or TG/PG policy rewrite.

## 10. Characterization And Documentation Ledger

| Commit | Scope |
|---|---|
| `cb713f3` | P9-00 inventory and prioritization |
| `0136482` | P9-00 zero-diff guard correction |
| `f50ec79` | S0-1 characterization |
| `dd84532` | S0-1 fixed-status documentation |
| `3b25d77` | PMB-9 characterization |
| `7f00253` | Phase boundary guard update |
| `e3f1427` | PMB-9 untouched-helper guard refinement |
| `2b866b2` | T1/PMB-30 semantic characterization |
| `994a8ca` | PMB-27 characterization |
| `2f88bdc` | PMB-27 fixed-status documentation |
| `65a9da5` | PMB-23B characterization and PMB-23A split |
| `74e91c4` | PMB-23B status/priority documentation |
| `71327c2` | PMB-17 characterization |
| `c92b849` | PMB-26A intentional self-heal audit |
| `2b37d84` | PMB-26B Telegram failure audit |
| `636ed48` | PMB-26C PostgreSQL failure audit |

## 11. Rollback Matrix

Characterization commits remain in place when a behavior fix is reverted.

| Ticket | Fix | Rollback command |
|---|---|---|
| S0-1 | `8ee0ea5` | `git revert 8ee0ea5` |
| PMB-9 | `f1d3cd2` | `git revert f1d3cd2` |
| T1-A / PMB-30 | `e4a4a2a` | `git revert e4a4a2a` |
| PMB-27 | `eb6c14f` | `git revert eb6c14f` |
| PMB-23B | `11d5283` | `git revert 11d5283` |
| PMB-17 | `32412d9` | `git revert 32412d9` |

## 12. Test Growth

Historical counts are taken from the Phase 8 closure commit `1f9eb00`; current
counts are measured during P9-10 closure verification.

| Suite | Phase 9 start | Phase 9 closure | Growth |
|---|---:|---:|---:|
| Position Manager | 689 passed | 763 passed | +74 |
| Execution | 394 passed | 421 passed | +27 |
| Full | 1946 passed, 1 skipped | 2069 passed, 1 skipped | +123 passed |

Closure baseline has one existing `PytestReturnNotNoneWarning` in
`test_state_rmw_golden.py`; skipped count remains one.

## 13. Dependency Audit

- T1-A fixed sequential ordering; T1-B exclusively owns concurrent idempotency.
- PMB-30's duplicate-ordering branch closed with T1-A; save-failure consistency
  belongs to T12 rather than reopening the closed branch.
- T14 described the same precheck move and is superseded by T1-A.
- PMB-23B fixed batch isolation without claiming PMB-23A atomicity.
- PMB-26A/B are intentional; B2 and C1/C2 retain delivery/durability policy.
- PMB-27 fixed non-final marker cleanup without solving PMB-4 stale-key design.
- No closed ticket depends on a deferred ticket to make its scoped assertion
  correct.

## 14. Architecture Invariants

- Exchange WS/REST remains runtime position authority; Redis remains PM state.
- PostgreSQL remains an optional event/episode ledger with no recovery read path.
- Marker self-heal, 30-second pending, and 24-hour seen behavior remain unchanged.
- TG remains best-effort; PG policy remains unchanged pending C1/C2 decisions.
- Ghost cleanup and silent reconcile remain separate channels.
- Phase 9 did not introduce framework, service, port, queue, or schema migration.
- P9-10 changes only documentation and closure guards; production diff is zero.

## 15. Next-Phase Backlog

| Priority | Ticket | Entry condition |
|---|---|---|
| P0 decision | T1-B | choose lock/idempotency scope across systems and sides |
| P0 design | T5 | define protection latency SLO and durable/retry model |
| P1 decision | PMB-23A | define durable recorder boundary and duplicate-vs-loss policy |
| P1 design | T12 | define crash-consistency and recovery authority |
| P1 decision | PMB-26C1/C2 | define PG durability, atomicity, retry, and batch policy |
| P2 decision | PMB-24 | define silent reconcile ownership |
| P2 decision | PMB-26B2 | define delivery acknowledgement and retry |
| P2 design | POS-ID | define canonical identity and migration |
| P3 design | PMB-4 | define race-safe stale/malformed marker cleanup |

PMB-29 and T14 do not enter backlog: they are `NO-RISK / KEEP` and
`INVALID / SUPERSEDED`, respectively.

## 16. Closure Criteria

All tickets have normalized status, owner, residual risk, and next action. All
fixed/intentional tickets have golden evidence. Production commits and rollback
commands are complete. There are no unknown or dangling characterization
results. Test and diff checks are green.

**PHASE 9 READY TO CLOSE.**
