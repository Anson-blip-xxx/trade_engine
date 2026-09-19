# P10 D2C-TRANSPORT - Injected Signed Verification Query

> **IMPLEMENTED / CLOSED as dormant isolated-V2 infrastructure.** The adapter
> contains no HTTP client, credentials, endpoint defaults, runtime caller, or
> import side effect.

## Outcome

`InjectedBinanceVerificationQueryAdapter` implements the coordinator query port
using an injected signed-GET callable. It performs exactly two symbol-scoped
queries in order: position risk, then open Algo orders. Only after both list
payloads are received does it sample the injected completion clock.

Transport exceptions and non-list Binance error payloads are typed separately
as position-query or Algo-query failures. Error reporting retains only public
`code`/`msg` fields and never stores headers, signatures, API keys, or secrets.

## Endpoint Safety

`BinanceVerificationEndpoints` requires two explicit absolute API paths. Paths
cannot contain query strings, fragments, whitespace, or be identical. The
adapter intentionally provides no `/fapi` or `/papi` default.

This is required because the current Binance documentation explicitly records
Portfolio Margin UM migration to `/papi/v1/um/algo/openAlgoOrders`, while this
repository currently uses standard USD-M `/fapi` helpers. A Portfolio Margin
path must not be guessed for a non-Portfolio account.

Official reference:
https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade

## Remaining Boundary

Deployment configuration must select and characterize the correct standard
USD-M endpoint for each PROD/DEMO environment. The existing `_light_fapi_get`
still swallows transport failures and is not wired to this adapter. Runtime
activation also requires default-off configuration, durable scheduling, and
operator-visible exhaustion/quarantine handling. R4B remains `IN_PROGRESS`.

**P10 D2C-TRANSPORT PASS.** The signed-query seam is explicit and testable;
endpoint approval and runtime wiring remain open.
