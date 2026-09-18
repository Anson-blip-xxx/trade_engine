# P10-07D / P10-D5A-3 - Episode Authority Onboarding Implementation

> Dormant production infrastructure only. **ACTIVE RUNTIME BEHAVIOR = 0 CHANGE.**

## 1. Scope

P10-07D adds single-slot, explicitly invoked onboarding primitives in
`position_identity/adoption.py`. The module answers whether one observed
exposure is safe to adopt, must be quarantined, conflicts with existing
authority, or is invalid. It can apply an approved plan through the D5A-2
authority-store abstraction.

It does not discover positions, scan Redis or exchange endpoints, mutate legacy
position payloads, create protection, close exposure, or wire any active flow.

## 2. Legacy Definition

`LEGACY_ACTIVE` means a real exchange/local exposure exists but the legacy
record has no canonical episode ID, slot generation, provenance, revision, or
canonical slot ownership. Legacy symbol, side, quantity, position ID, strategy,
and timestamps are evidence only. None is canonical authority.

## 3. Reconstructed Definition

`RECONSTRUCTED` means physical exchange exposure exists while native request
lineage, episode lineage, strategy ownership, and protection generation cannot
be recovered reliably. Reconstruction never invents S6, S8, or another owner.

Its only automatic persisted form is:

```text
provenance = RECONSTRUCTED
status = QUARANTINED
```

## 4. Classification

Pure classification uses typed `AdoptionClassification` values:

- `NATIVE_AUTHORIZED`
- `LEGACY_ADOPTABLE`
- `LEGACY_QUARANTINE`
- `RECONSTRUCTED_QUARANTINE`
- `ALREADY_ADOPTED`
- `ALREADY_RECONSTRUCTED`
- `CONFLICT`
- `INVALID`

`classify_legacy_active` and `classify_reconstructed_exposure` perform no IO and
write no authority state.

## 5. Evidence Contract

Legacy planning requires explicit evidence for exactly one slot:

- canonical `ExchangePositionKey`, including account principal and environment;
- supported current `ONE_WAY/BOTH` position mode and slot side;
- nonempty local symbol matching the canonical symbol;
- compatible local and exchange LONG/SHORT side;
- positive local and exchange quantity;
- explicit nonnegative quantity tolerance;
- nonempty legacy position ID alias;
- opaque candidate UUID;
- typed current authority read;
- explicit mixed-version deployment state;
- explicit authorization before initializing a missing authority key.

The plan stores normalized immutable `Decimal` quantities, tolerance, alias,
candidate UUID, and expected authority revision/generation/episode. It never
retains a mutable legacy position dictionary.

## 6. Quantity Consistency

The repository has no safe existing pure comparator for this migration
decision. Current order-rounding seams use binary float and include frozen
historical behavior where a lattice value can floor one step too far.

P10-07D therefore does not redesign exchange rounding and does not silently
reuse those seams. The caller supplies an explicit tolerance derived from the
already approved symbol precision/quantity policy. Both quantities are parsed
with `Decimal(str(value))` and are consistent only when:

```text
abs(local_quantity - exchange_quantity) <= quantity_tolerance
```

Missing, nonfinite, zero exposure, or malformed tolerance fails closed.

## 7. Adoption Planning

`prepare_legacy_adoption` returns immutable `AdoptionPlan` and never writes.
Safe FLAT authority captures exact expected revision, generation high-water,
and retained episode ID. A missing key is adoptable only when
`allow_authority_initialization=True`; absence alone never proves a virgin slot.

Insufficient evidence returns a typed quarantine decision rather than guessing
an episode. Malformed input returns `INVALID` and cannot reach the store.

## 8. CAS Apply

`apply_legacy_adoption(plan, authority_store, now=...)` accepts an injected
D5A-2-compatible store abstraction. For an explicitly authorized fresh slot it
first atomically initializes FLAT generation 0/revision 1, then calls
`adopt_if_unowned` with the exact expected state.

An absent-slot plan may continue only from that exact initial FLAT state. If
another episode was created and returned to FLAT between prepare and apply, the
old bootstrap plan conflicts instead of rebasing onto the newer high-water.

Success creates:

```text
status = ACTIVE
provenance = MIGRATED
slot_generation = previous generation + 1
revision = previous revision + 1
```

There is no direct Redis dependency and no alternate writer.

## 9. Concurrent Winner

Concurrent candidates use the D5A-2 revision/generation/episode/status CAS.
Exactly one candidate can become canonical. After non-APPLIED or unknown
acknowledgement, orchestration reloads authority. A losing candidate receives
`CONFLICT`, includes the canonical winner when readable, is marked discarded,
and is never retried against the winner's new generation.

If an unknown backend acknowledgement is followed by discovery of the exact
candidate, re-entry converges to the existing record. If unchanged FLAT remains,
the result stays `BACKEND_ERROR`; it is not converted to a business quarantine.

## 10. Legacy Alias

Successful migration preserves `legacy_position_id_alias`. The alias is a
lookup/diagnostic bridge only. The independently supplied opaque UUID remains
the canonical `episode_id`; no code derives one from the other.

## 11. Quarantine Reasons

`QuarantineReason` is typed and includes:

- `ACCOUNT_UNKNOWN`
- `SLOT_AMBIGUOUS`
- `EXCHANGE_LOCAL_MISMATCH`
- `AUTHORITY_CONFLICT`
- `AUTHORITY_MISSING`
- `MALFORMED_AUTHORITY`
- `MALFORMED_LOCAL_STATE`
- `MISSING_LEGACY_ID`
- `MIXED_VERSION`
- `BACKEND_UNAVAILABLE`

Unsafe legacy evidence produces a decision only. P10-07D does not invent a
MIGRATED or RECONSTRUCTED episode merely to persist a failed adoption.

## 12. Authority Existing Cases

| Current authority | Legacy result | Reconstruction result |
|---|---|---|
| absent, bootstrap denied | quarantine | conflict |
| absent, bootstrap authorized | initialize FLAT then CAS | initialize FLAT then CAS |
| FLAT generation N | adoptable, candidate N+1 | quarantined candidate N+1 |
| ACTIVE native | existing owner | conflict |
| ACTIVE migrated, same alias | already adopted | conflict |
| ACTIVE conflicting | conflict | conflict |
| QUARANTINED reconstructed | legacy quarantine | already reconstructed |
| malformed | invalid | invalid |
| backend unavailable | backend error | backend error |

No valid owner is overwritten.

## 13. Reconstructed Plan And Apply

`prepare_reconstructed_episode` requires a canonical slot, nonzero explicit
exchange evidence, candidate UUID, and typed authority read. It records no
strategy/system owner.

`apply_reconstructed_episode` initializes an explicitly authorized fresh slot
or uses captured FLAT high-water, then calls
`create_reconstructed_quarantined`. It can only create `RECONSTRUCTED` +
`QUARANTINED`; there is no automatic ACTIVE path.

It does not create/cancel SL, enqueue work, write an algo ID, call protection,
or mutate the legacy position dictionary.

## 14. Re-entry And Generation

Reclassifying an already MIGRATED ACTIVE record with the same legacy alias
returns `ALREADY_ADOPTED` without another CAS. Reclassifying an existing
RECONSTRUCTED QUARANTINED record returns `ALREADY_RECONSTRUCTED`.

Every new episode still allocates from retained FLAT high-water. A transition
from episode A generation 1 to FLAT and then reconstructed B creates generation
2. A stale A/adoption plan fails CAS after this ABA sequence.

## 15. Async Mutation Predicate

`can_mutate_async` is a pure future-wiring rule. It returns true only
when status is ACTIVE and episode ID, positive slot generation, and provenance
are complete. FLAT, QUARANTINED, absent, or incomplete authority returns false.

P10-07D does not call this predicate from the worker and does not mutate
protection. Before successful adoption, legacy work remains ineligible for
future fenced async mutation.

## 16. Emergency Close Separation

Emergency close remains authority-independent. Existing close and emergency
risk-reduction paths do not import onboarding and were not modified. This ticket
cannot make authority success a prerequisite for a full reduce-only emergency
close.

## 17. TOCTOU Limitation

The immutable plan protects authority ownership races through CAS, but dormant
P10-07D does not fetch or re-read exchange/local evidence. Future active wiring
must obtain fresh evidence, classify, acquire its mutation claim, re-read the
exchange exposure, then apply the captured plan. Changed evidence must abandon
the plan; the current module must not be treated as an exchange snapshot lock.

## 18. Dormant Wiring Proof

Architecture guards prove:

- no production module imports onboarding;
- open, close, protection, worker, monitor, reconcile, and state are unchanged;
- queue payload remains the legacy four-field tuple;
- position payload gains no episode fields;
- no runtime scan or startup migration exists;
- onboarding imports no PM, Redis client, protection, lifecycle, or reconcile;
- no `pm:positions` write or file fallback exists;
- reconstructed apply has no protection side effect;
- emergency close remains independent.

## 19. Backend And Malformed State

Backend unavailability remains `BACKEND_ERROR`/`BACKEND_UNAVAILABLE`; it does not
become a business conflict or trigger file fallback. Malformed authority is not
overwritten. Malformed local evidence never calls the store.

## 20. Next D3D-1B Integration

The D5A foundation is complete. P10-D3D-1B may next extend immutable queue
identity and drop legacy four-tuples before exchange mutation. D3D-1C worker
preflight/mutation claim and D3D-1D conditional writeback remain later tickets.
No P10-07D API is wired into those paths yet.

## 21. Rollback

`git revert <P10-07D commit>` removes onboarding helpers, exports, tests, docs,
and backlog closure. Because there is no active caller, rollback does not scan
positions, modify current Redis position state, perform adoption, create
protection, or affect trading.

## 22. Verification

Required verification covers Ruff, onboarding unit tests, Phase 10 architecture
tests, PositionManager, execution, full pytest, and `git diff --check`.

**P10-07D PASS.**

P10-D5A-3 and the D5A foundation are **IMPLEMENTED / CLOSED**. P10-D3D-1B is
`READY_FOR_IMPLEMENTATION`.
