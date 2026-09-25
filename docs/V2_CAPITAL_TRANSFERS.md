# Attested two-leg transfer accounting

Internal, isolated-QA only. No exchange calls, automatic fund transfer, runtime
deployment, credential change, venue identity verification or public API.

## Why two journals

A transfer can leave account A before reaching B. Recording both cash changes at
one timestamp gives an incorrect historical wallet balance. New `TRANSFER_OUT`
and `TRANSFER_IN` journals each retain the original venue event time and balance
against `TRANSFER_CLEARING`, rather than external capital or trading profit.
After both legs, clearing cancels in tenant/environment/currency consolidation.
Between event times cash is lower and clearing records the corresponding transit
amount. A single account's clearing balance is not its spendable asset. Existing
same-time `TRANSFER` journal behavior remains unchanged.

## Matching contract

`CapitalTransfers.match()` requires a tenant, explicit outgoing and incoming
immutable income IDs, and a unique evidence UUID. This is a trusted caller's
attestation, **not proof supplied by the exchange**. Retain the actual transfer
confirmation behind that reference. Never generate references and pair unrelated
events merely because their amounts or timestamps look alike. No fuzzy matcher or
automatic evidence verifier is implemented.

Both facts must be TRANSFER, have opposite nonzero equal amounts and one currency,
belong to distinct accounts under the same tenant/exchange/environment/product,
and have outgoing time no later than incoming time. Both must be strictly after
their own registered baseline cutoff. Missing or reversed baselines reject new
matches. Cross-cutoff, fee-deducted, different-currency, out-of-order and external
counterparty movements require an explicit separate accounting workflow; they
are not silently treated as deposits/withdrawals or matched with tolerances.

One transaction saves both balanced journals plus the immutable pair binding.
Database checks independently validate the source facts, ownership, signs,
currency, times, amount and journal source binding. Each fact may participate in
only one pair. Exact replay returns the original IDs; competing counterpart or
evidence reuse conflicts. A failure rolls back both cash legs and the pair.

An absent counterpart changes no cash in this book: the observed raw fact remains
available and wallet comparison exposes the difference/unresolved fact. This is
deliberately incomplete accounting until evidence arrives, not permission to
ignore the real transfer or continue spending a stale book balance.

## Projection integration and reversal

Apply `20260925_capital_transfers.sql` after the registry, journal and capital
income migrations, once transactionally. Then explicitly instantiate
`CapitalIncomeProjection(connect, include_transfers=True)`. The default remains
cash-only and does not require the new view. The chosen mode is saved in audit
results and wallet observation request fingerprints.

The opt-in receipt view includes both transfer legs. Matched facts replay instead
of being posted twice; missing pairs report `TRANSFER_COUNTERPARTY_UNMATCHED`.
A reversed leg makes **both** sides' projection and wallet checks unresolved;
the pair is never deleted, reused or silently reposted. Baseline reversal also
makes matching retries return `NEEDS_REVIEW`. Pair corrections must preserve the
original evidence and require a dedicated future correction workflow; this module
does not automatically undo the other leg or certify reconciliation.

As-of wallet checks use each leg's event time, including reversal time. Display
and daily report boundaries remain Asia/Shanghai; stored values remain epoch ms
and TIMESTAMPTZ. Do not shift epoch timestamps by eight hours.

## Acceptance still pending

- Automated venue evidence acquisition and identity verification.
- Durable unmatched-transfer aging, scheduling and tenant-specific alerts.
- External deposits/withdrawals, fees, cross-cutoff and multi-currency matching.
- Verified wallet coverage, cash-flow-aware risk integration and account switching.
- Authenticated/RLS-protected SaaS and real two-account Testnet acceptance.

This does not prove complete account cash coverage. `ATTESTED_MATCH` is deliberately
not named verified reconciliation; execution authorization remains false.

Validation on 2026-09-25: the final targeted regression passed 189 tests covering
transfers, income projection, journal/registry, capital policy/recovery, cash
ownership and settlement. Tests include UTC+8 midnight, concurrent duplicate
matches, transaction rollback, wrong-account/amount bindings and reversal review.
An earlier run exposed a stale expected status string after renaming unmatched
transfers; the assertion was updated and the entire targeted set rerun. Ruff and
staged whitespace checks passed. No runtime migration or service restart occurred.
