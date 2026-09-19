# P10 D3A - Typed Recovery Decision Engine

> **IMPLEMENTED / CLOSED as a pure dormant domain layer.** It performs no I/O,
> submits no order, mutates no Redis state, schedules no work, and is not wired
> into any runtime.

`decide_recovery` exhaustively maps every `OperationStage` to one typed
directive, an exchange-access ceiling, fencing/proof requirements, policy
requirements, and named pending work.

Safety rules encoded by the decision layer:

- `SUBMITTING` and `UNKNOWN` are query-only and require proof of effect absence
  before any later safe retry;
- only `INTENT_DURABLE` for an exchange-mutating operation can produce
  `CONDITIONAL_MUTATION`, and that is a preflight permission rather than an
  order submission;
- `GHOST_FINALIZE` verifies exchange-flat evidence and never becomes a close;
- `EXTERNAL_RECONCILE` remains query-only and requires an explicit disposition
  policy;
- confirmed effects replay local projection/finalization without exchange
  access;
- retryable and auxiliary stages expose exact durable pending task names but do
  not receive generic exchange permission;
- terminal, completed, and compensation-required stages do not auto-execute.

The operation model now requires object-shaped input/alias/effect JSON and a
unique normalized string array for pending requirements. `FAILED_RETRYABLE`
and `AUXILIARY_PENDING` require both named work and `next_attempt_at`;
`COMPLETED` cannot retain pending work. Matching JSON-type checks exist in the
dormant PostgreSQL schema.

Remaining work is directive-specific ports/executors, exchange evidence
resolvers, generation-fence validation, durable scheduler policy, and runtime
activation gates.

**P10 D3A-RECOVERY-DECISION PASS.**

