# Phase 9-09A - PMB-26C PostgreSQL Failure Propagation

> Characterization only. Production code has zero changes.

## 1. PG Semantic Role

The injected helper is `shared.postgres_client.record_trade_event(data) ->
bool`, wired as `ReconcileNotificationDeps.pg` by
`shared.position_manager._reconcile_service()`.

For an external position it writes one idempotent `trade_events` row with
`event_type=EXTERNAL_POSITION_DETECTED`. The payload contains the assigned
system and raw exchange position. `event_id` is deterministic and PostgreSQL
uses `ON CONFLICT (event_id) DO NOTHING`.

PG is an event/audit ledger side effect. It is not the exchange position source
of truth, reconciliation state store, or restart recovery source:

- Runtime position truth comes from exchange WS/REST and Redis metadata.
- No reconciliation decision reads `trade_events`.
- PostgreSQL may be disabled; the helper then returns `False`.
- Connection and SQL exceptions are caught by the helper and converted to
  `False`.
- The external-position caller ignores the returned boolean.

## 2. Helper Contract

| Property | Actual behavior |
|---|---|
| Helper | `record_trade_event(data)` |
| Table | `trade_events` |
| Parameters | one event dict; helper adds serialized payload and environment |
| Success return | `True` |
| Disabled/failure return | `False` |
| Connection/SQL exception | rollback when connected, then helper returns `False` |
| Pre-`try` argument/JSON error | propagates |
| Retry | none |
| Caller use of return | ignored |

The service seam nevertheless permits any callable. If that callable itself
raises, `notify_external_position` does not catch it and the exception escapes.
`dict(data)` and `json.dumps(payload)` execute before the helper's `try`, so a
malformed argument or circular payload can propagate. This distinction is
essential: an ordinary production PG outage normally returns `False`, while a
pre-connection preparation failure can exercise the raising topology.

## 3. Actual Call Graph And Order

```text
monitor_all
  -> state.load / PM _load
    -> WS or REST exchange snapshot
      -> _merge_and_save
        -> _load_meta
        -> _merge_meta_preserving_missing
          -> _merge_meta (raw insertion order)
            -> tradable-symbol check
            -> recent marker check
            -> marker clear, if present
            -> pop symbol from temporary metadata copy
            -> _notify_external_position, if exchange-only
              -> pending fingerprint check/write and return on first sighting
              -> 30-second grace check
              -> 24-hour seen check
              -> write seen
              -> clear pending
              -> normal external-position log
              -> TG attempt (exception swallowed)
              -> PG event callable (return ignored; exception propagates)
            -> add current symbol to temporary merged snapshot
          -> preserve local-only metadata
        -> save complete merged snapshot
```

Relative order is therefore:

`marker clear -> seen -> pending clear -> log -> TG -> PG -> merged assignment
-> whole-snapshot save`.

PG is after marker, alert-state writes, logging, and TG, but before the current
symbol is added to the merged snapshot and before the final state save.

## 4. Success Baseline

- PG is called exactly once after an eligible 30-second observation.
- The function returns `None`.
- Event IDs are `external:{symbol}:{side}:{entry}:{qty}`.
- Payload includes `system` and the raw exchange position.
- PG runs after TG.
- A completed batch is merged and saved normally.

## 5. Return-Value Semantics

`None`, `False`, `True`, and an arbitrary success object have identical caller
behavior. Only an exception from the injected callable changes control flow.

This means the concrete helper's `False` failure signal is currently treated as
success by external-position reconciliation.

## 6. Raised-Exception Reproduction

With a deterministic callable that raises `RuntimeError`:

1. The exception propagates to the caller; normal return disappears.
2. TG has already been attempted.
3. Seen has already been written.
4. Pending has already been cleared.
5. The normal external-position log has already been written.
6. There is no PG-specific failure log at this layer.
7. The current symbol has not yet been assigned to the temporary merged dict.
8. The final `pm:positions` snapshot save is not reached.

## 7. Failure Atomicity

A truly raising PG callable produces a partial-commit topology:

| Side effect | State when PG raises |
|---|---|
| Closed-marker self-heal | already committed; no rollback |
| Seen key | already written |
| Pending key | already cleared |
| PM log | already emitted |
| TG | already attempted |
| PG | attempted and raised |
| Current symbol merged | not completed |
| Whole `pm:positions` save | not executed |

The top-level metadata dictionary is copied before `meta.pop`, so the aborted
temporary merge does not directly remove persisted local metadata. Redis marker
and alert-key writes are separate side effects and survive the failure.

## 8. Marker And State Effects

Marker self-heal remains PMB-26A behavior and occurs before notification. PG
failure does not restore the marker. Thus self-heal may survive an aborted
merge.

For exchange-only positions, local state is not adopted before PG. If PG raises,
the current symbol is absent from the incomplete merged dict and `_save` is not
called. Earlier symbols exist only in that temporary dict; their alert and PG
side effects survive, but their merged snapshot is not persisted by this call.

## 9. Seen, Pending, And Retry

After PG raises:

- seen contains the failed event fingerprint and timestamp;
- pending is `{}`;
- the next observation recreates pending and enters the 30-second grace;
- after grace, the 24-hour seen guard suppresses TG and PG;
- another PG attempt is possible only when seen is at least 24 hours old, or
  when the fingerprint changes.

The concrete helper's `False` result has the same alert-key outcome, so an
ordinary failed PG write also has no immediate retry path.

## 10. Batch Behavior

For ordered exchange-only symbols A, B, C, with A PG success and B PG raise:

- A completes its notification side effects and temporary merge assignment.
- B clears its marker, writes alert state, logs, attempts TG, then raises in PG.
- C is not visited.
- `_merge_and_save` does not save the temporary A/B/C snapshot.
- A's successful PG event and A/B marker/alert/TG effects are not rolled back.

This occurs during the initial `monitor_all` state load, before its later
per-position isolation loop. Therefore that loop cannot isolate the exception.
In the strategy runtime the monitor lock is released by `finally`, then the
outer strategy loop emits its generic main-loop error. There is no PG-specific
log in the notification chain.

When A, B, and C each return `False` instead of raising, all three continue and
the merged snapshot is saved. This is the behavior of ordinary failures from
the currently wired helper.

## 11. TG Contrast

| Channel | Exception | Return value |
|---|---|---|
| TG transport | swallowed inside notification | ignored |
| abstract PG callable | propagated | ignored |
| concrete PG helper | DB exceptions become `False`; pre-`try` preparation exceptions propagate | `False` ignored by caller |

The apparent TG/PG asymmetry is therefore only partly real. The service seam is
fail-fast for a raised PG exception, while the production adapter contract is
best-effort and fail-open for ordinary database failures.

## 12. Durability Evidence

Evidence for stronger durability intent:

- `shared/postgres_client.py` calls configured PostgreSQL the "transactional
  source of truth".
- The schema and deterministic IDs provide transactional, idempotent event
  persistence.
- Reconcile comments explicitly state that PG exceptions propagate.

Evidence for optional historical-ledger intent:

- `execution/ports/ledger.py` calls PG an optional historical ledger, not a
  runtime source of truth.
- `LEDGER_BOUNDARY_INVENTORY.md` documents no PG read/recovery path.
- Trading remains functional without PG configuration.
- Helpers swallow failures into `False`, and runtime callers ignore the value.
- Redis/exchange, not PG, drives reconciliation decisions.

The repository therefore does not establish whether an external-position PG
write is required for reconciliation correctness.

## 13. Risk Assessment

- A real database failure can silently lose the audit event for at least 24
  hours because `False` is ignored after seen is written.
- A nonstandard/injected PG callable that raises can abort the entire exchange
  snapshot merge, skip later symbols, and leave marker/alert side effects
  committed without saving local state.
- Making PG fail-fast would increase monitoring blast radius.
- Swallowing all PG exceptions would preserve availability but could weaken an
  intended audit guarantee that is not clearly specified.

## 14. Conclusion

**PMB-26C = NEEDS_PRODUCT_DECISION.**

Code cannot answer: "When the PG event write fails, should external-position
reconciliation count as successful?" The current abstraction and concrete
helper implement conflicting policies. P9-09A does not swallow, wrap, retry, or
otherwise change either behavior.

## 15. Follow-Up Split

- **PMB-26C1 - PG failure policy:** choose fail-fast durability versus explicit
  best-effort observability, including whether `False` must be checked.
- **PMB-26C2 - partial-commit/retry semantics:** decide marker, seen/pending,
  batch isolation, and retry behavior after a failed durable write.

Both remain deferred pending product requirements.
