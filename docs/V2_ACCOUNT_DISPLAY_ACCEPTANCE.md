# Account switching and display consistency

## Corrected behavior

The home dashboard previously aggregated every Testnet account. The account
console defaulted to the first registration on every visit. Neither represented
the controller's confirmed execution binding.

The deployed dashboard now requires a server-side tenant binding and resolves
the actual ACTIVE/DRAINING route in a repeatable-read transaction. Summary,
positions, trades, settlement curve, daily cash/closed-trade reports, health and
trade detail access are limited to that account. A pending request does not
change reporting scope before actual activation. Missing confirmed context
fails closed rather than falling back to all accounts. The read-only role gains
only nonsecret registry/route/alias columns, not vault data or worker tokens.

Raw market signals are shared. Their processed/intent/fill counts and displayed
consumer decisions are account-specific; the UI explicitly labels this split.
Historical account data is preserved, not deleted to make the new account empty.

The account console initially selects the actual execution account, regardless
of registration order. Statistics selection still does not activate trading.
The dashboard labels its account and phase; failed reads clear old statistics.
Navigation restoration refreshes context, and late trade-detail responses are
discarded after account changes or drawer closure.

## Real-browser acceptance

A temporary Chromium session exercised deployed Web assets and APIs through a
loopback-only request adapter (not a test of external Basic Auth or TLS):

- Select the other Testnet account and click the actual switch control.
- Wait for confirmed controller ACTIVE, not just successful request submission.
- Return home and compare displayed metrics/account against the scoped API.
- Return to account management and reload; selection remains the actual account.
- Repeat back to the starting account.
- Inject an overview 503 in the browser; old metrics and rows disappear.
- No page JavaScript errors.

Both directions passed. New entries stayed disabled throughout; no LIVE account
was activated. This tests account switching/display consistency, not strategy
profitability or the full signal-to-trade lifecycle.

## Deployment

Set `V2_DASHBOARD_TENANT` outside Git and apply
`deploy/v2-testnet/dashboard-account-scope-grants.sql`. Missing configuration
prevents dashboard startup. Web-only release changes do not require restarting
the trading controller. The systemd credential adapter belongs to services,
not the file-I/O-free authoritative core; its protected mount validation remains
unchanged.
