# Testnet owner enrollment acceptance — 2026-09-27

Result: partial read-only acceptance, **not execution-switch acceptance**.

The deployed registry contained two SANDBOX enrollments total: one imported
historical binding and one Web-enrolled binding. The LIVE enrollment was excluded
from all credential resolution and exchange requests.

- Imported Testnet binding: encrypted resolution succeeded, signed GET rejected
  with HTTP 401 / exchange -2015, consistent with previous acceptance. Exact
  cause remains unproven; do not infer key validity from worker process liveness.
- Web-enrolled Testnet binding: signed position-mode GET accepted. Additional
  read-only snapshots reported canTrade=true, one-way position mode, single-asset
  margin mode, positive available balance, two nonzero positions, no ordinary
  open orders and one open conditional order. These counts are point-in-time,
  not a durable reconciliation or proof of ownership/protection coverage.
- A successful signed read does not establish distinct exchange account identity
  or prove order placement, stop protection, close, settlement or safe switching.

Every exchange request used the fixed SANDBOX transport with trading,
cancellation, protection and settings writes disabled, and the existing scoped
database quota. Signed-read outcomes were persisted as immutable credential-check
receipts; no raw credentials, signed URLs, account balances or position payloads
were persisted by the probes. No running worker was switched or restarted.

Before activation: reconcile existing positions and conditional orders with
historical ledger/account identity; resolve the unusable source binding; implement
and qualify exclusive worker fencing and safe handover. Do not silently assume
the Web enrollment represents a new empty exchange account. The original worker
still uses its legacy credential source; successful Web enrollment does not
repair that worker's private access automatically.

Isolated QA: 56 credential acceptance/runtime binding/console/frontend tests and
24 switch-preparation/draining tests passed. These are controlled QA cases, not
an exchange-side end-to-end order or A-to-B activation test.
