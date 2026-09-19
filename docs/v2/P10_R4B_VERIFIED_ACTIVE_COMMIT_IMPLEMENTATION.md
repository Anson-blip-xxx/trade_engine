# P10 R4B-PROTECTION-COMMIT - Verified ACTIVE CAS

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No runtime
> caller is wired. The adapter does not query Binance, enqueue work, execute a
> trade, or modify legacy snapshot behavior.

## Outcome

`RedisVerifiedActiveCommitAdapter` is the only boundary in V2 that may turn a
desired-protection record `ACTIVE`, and only after the strict exchange evidence
core returns `VERIFIED`.

The adapter reads the exact raw authority, projection, and desired records,
strictly parses them, verifies the supplied bounded exchange observations, and
builds a legal desired-state transition. One Lua operation then compares all
three raw tokens before writing only the desired key.

Any authority, projection, or desired mutation between verification and commit
returns `STALE` with no write. Failed verification returns `NOT_VERIFIED` and
does not call Lua.

## Acknowledgement And Recovery

Results distinguish `APPLIED`, `ALREADY_ACTIVE`, `NOT_VERIFIED`, `STALE`,
`NOT_FOUND`, `MALFORMED`, `INVALID`, `UNAVAILABLE`, and `UNKNOWN`.

An exception after Lua is `UNKNOWN`. If that operation actually committed, an
exact retry recognizes the durable `ACTIVE` record by `last_operation_id`,
rechecks all three raw tokens atomically, and returns `ALREADY_ACTIVE` without
requiring the now-older exchange observation. An ACTIVE record written by a
different operation still requires fresh verification evidence.

## Remaining Boundary

This commit records a verified observation; it cannot make the external order
remain active after observation. Runtime use still needs a characterized
Binance payload adapter, bounded query/reverification policy, selected caller
ACK propagation, and restart-safe desired-state reconciliation.

R4B remains `IN_PROGRESS`. The current main/runtime `_save`, monitor,
reconcile, migration, and protection worker paths are unchanged.

## QA Contract

Disposable-Redis tests cover verified transition, unverified zero-write,
authority/projection/desired races, ambiguous post-commit recovery, foreign
operation freshness, missing records, and backend failure. Fact tests freeze
three-token comparison, desired-only mutation, verification-before-Lua,
same-operation recovery, dormant wiring, and roadmap status.

**P10 R4B-PROTECTION-COMMIT PASS.** Verified ACTIVE persistence is complete as
dormant infrastructure; exchange normalization/reverification and runtime
lifecycle wiring remain open.
