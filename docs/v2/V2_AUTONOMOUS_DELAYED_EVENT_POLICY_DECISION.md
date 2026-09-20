# V2 Autonomous Delayed-Event Safety Policy

> **PRODUCT DEFAULT APPROVED / DORMANT CONTRACT IMPLEMENTED.** This policy was
> approved on 2026-09-20. It performs no scheduling, exchange call, persistence,
> notification, or runtime activation.

## Approved defaults

1. Risk-increasing work has a short validity window. After it expires, cancel
   the old request; never replay it automatically.
2. An `UNKNOWN` mutation is query/reconcile only. It may be quarantined after
   the isolation deadline, but it is never blindly retried.
3. Any context change invalidates old work. The old event becomes audit
   evidence and a new decision must be built from current facts. It cannot be
   rebound to a later episode.
4. An unprotected current position first attempts protection restoration, then
   pauses new opens, then enters quarantine while restoration continues.
5. Automatic market close is disabled by default. A deployment must explicitly
   opt in before the policy can produce an `EMERGENCY_REDUCE_ONLY` candidate.
6. External positions remain quarantined/query-only and never inherit native
   lineage automatically.
7. Operator approval is version-bound and expires. A late approval must not
   execute the old plan; current evidence creates a new approval request.

## Time policy

There are deliberately no hidden production durations. Each activation
manifest must provide positive values for:

- short retry/validity window;
- isolation deadline;
- emergency deadline;
- operator-approval wait window.

The first three must be strictly increasing. Choosing their production values
is an SLO/configuration decision and does not change the safety semantics.

## Context and approval binding

The event context digest must be built from a canonical snapshot containing at
least the exchange-position key, operation ID/version/type, episode ID, slot
generation, protection generation, authority/desired revisions, exchange query
time, position direction and canonical quantity, and relevant order/algo
aliases and statuses.

An `ApprovalBinding` additionally stores the operation type, key
identity/version fields, policy version, evidence observation time, position
direction, canonical quantity, and approved action explicitly. Its evidence
digest covers remaining aliases/statuses. A change to any binding field
produces `CONTEXT_MISMATCH`; `expires_at` is exclusive.

Digest equality is evidence correlation, not side-effect permission. Protection
restoration and emergency reduce-only remain mutation candidates requiring a
fresh generation revalidation, valid operation/slot ownership, exchange
evidence, and a directive-specific executor.

The policy function accepts the current canonical digest and compares it
itself; callers do not supply a trusted `context_matches` boolean. An approval
also cannot be issued before the exchange evidence it references.

## Implemented pure contract

`operation_journal.delayed_policy` provides:

- typed delayed event/action decisions;
- explicit caller-supplied timing thresholds;
- stale-context rebuild behavior;
- default-off emergency close;
- immutable, expiring operator approval bindings;
- decimal quantity canonicalization for evidence construction.

**V2-AUTONOMOUS-DELAYED-EVENT-POLICY PASS.**
