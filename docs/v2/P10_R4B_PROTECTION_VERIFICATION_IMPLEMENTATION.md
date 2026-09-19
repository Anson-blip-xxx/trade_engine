# P10 R4B-PROTECTION-VERIFY - Strict Exchange Evidence Core

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** This is a
> pure verification core. It does not call Binance, parse an uncharacterized
> API payload, write Redis, transition desired state to `ACTIVE`, enqueue work,
> or alter any runtime path.

## Outcome

`verify_current_protection` is the first implementation boundary that can
produce the strong D2 `VERIFIED` fact. It requires one bounded evidence set:

- ACTIVE slot authority;
- canonical projection and desired protection with the exact same slot,
  episode, and slot generation;
- fresh exchange exposure matching projection side/quantity and desired
  covered quantity;
- a desired-state exchange alias;
- one fresh active exchange `STOP_MARKET` observation for that alias;
- exact reduce-only, closing-side, trigger-price, and covered-quantity match
  within explicit caller tolerances.

Only exchange statuses `NEW` and `WORKING` are treated as active protection.
`TRIGGERED`, an ACK containing `algoId`, a local alias, or a symbol-only match
does not prove protection.

## Ambiguity And Fail-Closed Rules

One desired generation may retain terminal aliases from prior attempts, but it
must have exactly one matching active alias. Multiple active aliases are
ambiguous. Any additional active `STOP_MARKET` order for the same physical slot
also makes the result ambiguous, even when the desired alias itself matches.

Evidence observed before the latest desired revision, in the future, or older
than the explicit age bound is stale. FLAT/QUARANTINED authority, terminal
desired state, identity mismatch, exposure mismatch, missing alias, inactive
order, and any specification mismatch all fail closed.

## Compatibility Boundary

Typed `ExchangeExposureObservation` and `ExchangeProtectionObservation` are an
adapter boundary, not claims about the current Binance response schema. D2C
must characterize the live/testnet query contract and normalize it into these
types before runtime use.

The verifier returns evidence only. A later atomic commit must compare the raw
authority, projection, and desired tokens before transitioning the exact
desired generation to `ACTIVE`. Until that commit and selected lifecycle
caller wiring exist, R4B remains `IN_PROGRESS` and non-deployable.

## QA Contract

Pure tests cover exact verification, ACK-without-active rejection, canonical
identity mismatch, inactive authority, terminal desired state, stale evidence,
exposure mismatch, missing/unobserved alias, every order-spec field, triggered
status, multiple active aliases, foreign active STOP ambiguity, retained
terminal aliases, and invalid tolerance bounds.

Phase 10 fact tests freeze the active-status set, exact identity/alias checks,
freshness requirements, pure/no-I/O boundary, dormant wiring, and backlog gate.

**P10 R4B-PROTECTION-VERIFY PASS.** The evidence core is ready; Binance payload
characterization, atomic verified-ACTIVE commit, and runtime lifecycle wiring
remain open.
