# Scoped Testnet exposure reconciliation

`services/v2_testnet_scoped_cleanup.py` is an operator-only utility, not an
automatic retry worker or a LIVE trading entry point. It requires an explicitly
approved scope, symbol, case UUID and exact native-protection IDs. Case reuse
cannot change the approved symbol or protection set.

## Safety protocol

1. Stop the trading runner and acquire the existing account advisory lock.
2. Validate one-way mode, the precise remaining position, no ordinary orders,
   and the approved close-position protection set.
3. Persist a case plan and claim the action in PostgreSQL before a reduce-only
   MARKET submission. The client identity is deterministic for the case/symbol.
4. On timeout or restart, query that identity; never resend the write. Keep
   protection while the close remains unconfirmed.
5. Verify the actual FILLED order and complete, identity-matched fills before
   checking flatness. Only then consider exact-ID protection cancellation.
   Already-terminal protection needs no DELETE.
6. Recheck position and ordinary orders around the conditional-order check;
   append a timestamped flat-verification receipt to PG audit history.
7. Restore the normal runner. Existing account safety gates remain in force.

This protocol reduces the approved physical exposure. It does not prove a
historical UNKNOWN request failed and does not rewrite that request to REJECTED,
CANCELLED or FILLED. It cannot, by itself, authorize new opening trades.

## Verification and remaining accounting work

The pre-execution scoped/legacy cleanup and submission-diagnostics selection
passed 47 tests. After adding durable flat-verification receipts, the scoped and
legacy cleanup regression passed 24 tests. Covered cases include response loss,
restart without resubmission, changed positions, unapproved protection, a running
daemon, case rebinding and LIVE rejection.

An approved Testnet incident was physically closed and independently verified
flat with no target ordinary/conditional orders. Its venue order, commissions
and fill evidence are retained in the separate maintenance journal; all runtime
services were restored. No historical rows were deleted or falsified.

**Not yet complete:** adopting this maintenance fill into the strategy episode,
explicit administrative disposition of the unresolved request, complete fee and
funding reconciliation, and corresponding performance/notification attribution.
Until those gates are implemented and tested, the old UNKNOWN and account opening
block remain. A future disposition must preserve the original uncertain venue
outcome and prevent a late reduce-only request affecting a future position; merely
changing a terminal status or ignoring the alert is not an acceptable fix.
