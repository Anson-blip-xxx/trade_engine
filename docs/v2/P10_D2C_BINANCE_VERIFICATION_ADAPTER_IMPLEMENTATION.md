# P10 D2C - Binance Verification Normalization And Retry Policy

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** No network
> request or runtime caller is included.

## Official Contract Baseline

The September 2026 Binance documentation describes the current Algo response
with `algoId`, `algoType`, `orderType`, `symbol`, `side`, `positionSide`,
`quantity`, `algoStatus`, `triggerPrice`, `closePosition`, and `reduceOnly`.
It also documents the prior conditional endpoints and their field names as
deprecated. This adapter accepts only the explicit new schema and never guesses
legacy aliases.

Reference:
https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade

## Normalization Boundary

`normalize_binance_verification_snapshot` consumes already-fetched
`positionRisk` and open Algo-order payloads plus the local query-completion
timestamp. It performs no I/O.

- the exact symbol and `positionSide` must identify one position row;
- `positionAmt` must be finite and nonzero;
- every relevant Algo row must contain the complete new-schema field set;
- `algoType` must be `CONDITIONAL`;
- quantity verification does not accept `closePosition=true`;
- `reduceOnly` must be an actual JSON boolean;
- numeric strings are parsed strictly and must be finite/positive;
- unrelated symbol/slot rows are ignored.

Evidence freshness uses the local completion time of the bounded query, not
the order's historical `updateTime`.

## Bounded Reverification

`VerificationRetryPolicy` requires an explicit positive attempt budget and
elapsed deadline plus bounded exponential delay. `STALE_EVIDENCE`,
`ORDER_NOT_FOUND`, and `ORDER_NOT_ACTIVE` may query again. `VERIFIED` proceeds
to the atomic commit. Identity, authority, desired-state, exposure, order-spec,
alias, or multiple-order conflicts quarantine immediately.

Once either attempt or time budget is exhausted, the result is `EXHAUSTED`;
the policy never silently extends the deadline or turns failure into success.

## Remaining Boundary

The active endpoint selection, signed transport, rate-limit handling, and
testnet/mainnet characterization remain outside this node. Runtime wiring must
inject query results, honor the typed retry decision, and propagate the final
verified-ACTIVE commit acknowledgement. R4B remains `IN_PROGRESS`.

**P10 D2C NORMALIZATION PASS.** Strict schema normalization and bounded retry
policy are ready as dormant infrastructure.
