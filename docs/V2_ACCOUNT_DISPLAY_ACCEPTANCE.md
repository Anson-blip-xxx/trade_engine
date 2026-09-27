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

## Regression results

The full isolated `tests/v2_core` run completed 1,733 tests: 1,732 passed and one
failed the no-file-I/O core boundary because the systemd credential adapter was
in `v2_core`. Moving that adapter to services fixed the violation without
weakening the rule. The subsequent 79-test focused run passed, including core
boundary, runtime credentials, credential import, dashboard account isolation,
performance reports, account frontend, execution routes and controller tests.
The entire 1,733-test suite was not rerun after that move; do not describe the
original full run as all-green. Final deployed Web assets were separately
retested in Chromium through both switching directions and failure clearing.
External unauthenticated requests to home, accounts and their APIs return 401.
The dashboard role cannot read vault data/worker tokens or update routes.

## Deployment

Set `V2_DASHBOARD_TENANT` outside Git and apply
`deploy/v2-testnet/dashboard-account-scope-grants.sql`. Missing configuration
prevents dashboard startup. Web-only release changes do not require restarting
the trading controller. The systemd credential adapter belongs to services,
not the file-I/O-free authoritative core; its protected mount validation remains
unchanged.
