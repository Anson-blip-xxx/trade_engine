# Account switch preparation

Internal SANDBOX-only orchestration; not a completed A/B switch or public API.
Apply `20260925_account_switches.sql` after tenant registry/base core migrations.
The capital journal migration also supplies the registry identity immutability
guard used by the isolated acceptance fixture. Runtime migrations are not applied.

`AccountSwitches.request` pins source/target registry credential versions and
claims both accounts. Tenant-scoped request IDs are idempotent, with conflicting
endpoints rejected. Claims prevent overlapping orchestrations, including reverse
switches; they do not fence unrelated execution workers or verify venue identity.

Explicit compare-and-swap actions:

- `REQUESTED -> CANCELLED`: releases both claims without touching opening gates.
- `REQUESTED -> DRAINING`: checks pinned versions and commits source opening drain,
  switch state and immutable audit event in one PostgreSQL transaction.
- Replaying the successful action with its original expected version returns the
  recorded result. Use `inspect` for current binding/readiness diagnostics.

There is deliberately no cancellation after draining, automatic unhalt, completed
state, target activation, or claim release after draining. Pending source permits
remain subject to venue reconciliation; zero local pending orders does not prove
flatness. Existing target workers are not stopped by a preparation claim.

Account rows are held with shared locks while validating/draining, preventing
concurrent credential rotation from invalidating the check before commit. Rotation
before draining blocks that action; cancellation and a fresh request are possible.
Rotation after draining does not remove the source gate and is visible in inspect.
Tenant locks serialize orchestrations; account risk locks fence opening permits.
Application methods enforce atomic claims/events/gates; database history guards
are not a replacement for restricted DB roles or public API authentication.

Remaining activation gates are explicit and always unresolved in this component:
venue identity verification, target readiness, and exclusive execution ownership.
UUID references are not authorization or proof. All workers must support the drain
gate before operational use; older binaries ignore it. No running service is
upgraded and no current trading account is gated by delivering this module.

QA covers concurrent/idempotent requests, competing drain/cancel, cross-tenant and
LIVE rejection, credential version changes, immutable history, and forced audit
failure rolling back both the source gate and transition. Run:

```sh
bash scripts/qa_v2_data_core.sh -q tests/v2_core/test_v2_account_switches.py tests/v2_core/test_v2_account_draining.py tests/v2_core/test_v2_account_registry.py
```

Validation: expanded isolated regression including those three files plus
`test_v2_account_risk.py`, `test_v2_intent_admission.py` and
`test_v2_runtime_policy.py`: **280 passed**. Ruff and whitespace checks pass.
This is a targeted regression, not a new full-core suite or venue acceptance run.
