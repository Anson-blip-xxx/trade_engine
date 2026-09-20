# P10 D3B/D3D - Recovery Admission Coordinator

> **IMPLEMENTED / CLOSED as a dormant admission boundary.** It starts no
> worker, acquires no lease or mutation claim, invokes no directive handler,
> calls no exchange API, and mutates no backend.

`RecoveryAdmissionCoordinator` composes one immutable `RecoveryWorkItem` with
the injected D3D generation resolver. It re-derives the recovery decision from
the operation before trusting it, then produces one of six typed states:

- `READY`: only a local or query-only directive may proceed to its still-
  missing executor;
- `MUTATION_OWNERSHIP_REQUIRED`: generations match, but a conditional exchange
  mutation remains prohibited until the approved slot claim/lease protocol is
  composed;
- `POLICY_GATED`: compensation or another policy-owned decision is not
  automatically admitted;
- `GENERATION_REJECTED`: canonical identity is stale, blocked, or changed;
- `DEPENDENCY_UNKNOWN`: a required read is not trustworthy;
- `INVARIANT_VIOLATION`: an injected response or internal contract is invalid.

The coordinator can revalidate an admitted witness, but successful
revalidation preserves `MUTATION_OWNERSHIP_REQUIRED` for mutating work. No
result from this module is standalone permission to submit, cancel, replace,
write back, or advance the operation journal.

## QA boundary

Tests cover forged decisions, policy short-circuiting, fence-free local work,
query-only work, conditional mutation refusal, typed generation failures,
adapter exceptions, invalid responses, missing witnesses, exact revalidation,
and changed witnesses. Architecture facts prohibit concrete Redis/PostgreSQL,
Binance, runtime, executor, thread, and service dependencies.

## Remaining work

- D3C typed exchange evidence resolver after endpoint/product approval;
- operation- and slot-scoped mutation ownership composition;
- directive-specific executors and durable post-effect journal transitions;
- default-off scheduler/runtime activation and fault-injection QA.

**P10 D3B/D3D-RECOVERY-ADMISSION PASS.**
