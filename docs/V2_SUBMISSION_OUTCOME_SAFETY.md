# Submission outcome safety

## Implemented

- Preserve allowlisted transport category, integer HTTP status and integer venue
  error code in UNKNOWN order events. Never store response text, exception text,
  credential-bearing URLs or arbitrary exception attributes.
- Only an exact HTTP 400 synchronous submit response with one of the explicitly
  reviewed parameter/signature/timestamp validation codes can become REJECTED.
  Evidence says submission_sent=true: this is different from a local preflight
  rejection, where submission_sent=false.
- Network failures, timeout codes, 5xx, unrecognized responses, duplicate-order
  rejection codes and query-time order-not-found remain ambiguous. No automatic
  resend and no release of UNKNOWN order risk reservations.
- Existing UNKNOWN events are not rewritten or reclassified using new rules.

References reviewed: [USD-M error codes](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code)
and [general API semantics](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info).
The allowlist is a protocol safety boundary, not an operator-tunable risk knob.

## Historical incident boundary

Read-only checks of the existing affected Testnet request found order-not-found,
no fills in the checked submission window and a remaining position. Original
submission error details were not retained; the service journal also has no
entries in the incident window. These observations do not prove the original
request was rejected. No order has been resubmitted, falsely finalized or erased.

Further recovery needs definitive venue evidence for that request, or a separately
authorized and audited exposure-reconciliation operation. Such an operation must
fence entries, inspect all outstanding native/regular orders, preserve protection,
reconcile executions and retain the original uncertainty in audit history. It
must not simply set the old order to REJECTED or erase its reservation.

The first roadmap gate is therefore not closed. SaaS registration remains an
internal, non-activated foundation; no claim of complete multi-account acceptance
or restored end-to-end opening is made.
