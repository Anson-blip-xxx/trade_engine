# Shared market / TradingView integration

Implementation date: 2026-09-27, display timezone UTC+8. Testnet only.
This feature does not grant trading authority, switch accounts or enable LIVE.

## Coverage and data ownership

`trade-v2-market-hub.service` owns public market collection independently of the
active account worker. It has no exchange credentials or order transport.
`exchangeInfo` selects every TRADING USDT PERPETUAL contract; batch size is a work
budget, not a symbol whitelist. Catalog refresh defaults to 300 seconds and the
catalog fails closed after 900 seconds. Quarterlies and other quote assets are
not silently treated as USDT perpetuals.

On the date above the public Testnet directory contained 528 eligible contracts.
524 passed the existing execution adapter's symbol grammar. Four non-ASCII names
are explicitly listed in `unsupported_symbols`; they are not advertised as
tradeable. Supporting these requires end-to-end symbol validation changes,
including order signing, protective orders and TradingView normalization.

The hub fetches a bulk lightweight 24h ticker every 30 seconds and ranks movers.
After the first observation it weights ticker-to-ticker price change ahead of
absolute 24h change; configurable short/day weights prevent yesterday's large
movers from permanently occupying today's fast queue. Invalid individual ticker
rows are excluded from ranking, not treated as valid prices.
It reserves background slots for fair coverage, while prioritizing the configured
regime benchmark basket, held risk reservations and unexpired TV/demand symbols.
The first full history is 1440 closed one-minute candles. Subsequent refreshes
request only missing closed minutes. Incomplete history, invalid bars and future
timestamps fail closed for that symbol; one failed symbol does not stop others.
This release uses bounded REST polling, not WebSocket delivery.

PostgreSQL stores catalog, policy versions, symbol progress, shared budgets,
durable signals and decision evidence. ClickHouse stores incremental candles with
content digests. Redis contains reconstructible rolling history and S0/S3
projections. No local JSON/CSV business-state store is introduced. Archive writes
are verified before publishing; cache loss reconstructs the history from CH.
Conflicting archive duplicates are rejected, not hidden by ReplacingMergeTree.
Redis updates use monotonic CAS and reject same-time conflicting content.
Candles from each bounded cycle are inserted as one CH batch to avoid hundreds
of one-row parts/minute. Real driver FixedString hashes are normalized from bytes
before comparison, with the same behavior covered by regression tests.

S3 lifecycle state is partitioned by symbol, allowing independent publications
at the same minute without the old global monotonically-increasing frame clash.
S0 uses a configurable, same-minute benchmark basket (initially BTC, ETH, SOL,
BNB). Its evidence explicitly labels benchmark breadth; it is not claimed to be
full-universe breadth, nor is that basket an admission whitelist.

## TV and internal candidate integration

`V2_SHARED_MARKET=true` composes the shared service into an account worker. In
that mode `V2_SYMBOLS` is unused and need not be set. The legacy mode remains for
rollback and existing tests; its limits must not be confused with the new mode.

Both TV and S3 consult a fresh catalog and use the same fresh S0/S3 market gates
and the same account-specific capital, protection and sizing checks. A TV symbol
outside the former list requests priority warmup. If its context cannot be made
ready before the original expiry, it expires; no timestamp refresh/backfill is
allowed. A delisted/unsupported contract has an explicit rejection receipt.

Frozen decisions include the catalog digest and recent opposite-source signal
identities, classified as confirming/conflicting. Confirmation does not silently
increase size or override a risk rejection. TV is not required to wait for an
identical internally generated event in order to be evaluated.

For the same account/strategy, symbol and direction, cross-source OPEN decisions
share a rolling opportunity window (default 60 seconds). A transaction advisory
lock covers check and immutable first-decision insertion. A subsequent correlated
signal links to the winning decision and is ignored rather than opening twice.
Committed decisions are reused after a crash; existing risk reservation and
order idempotency remain the final execution protections.
The opening risk reference also uses the new per-symbol PG frame receipt and
reconstructed CH candles. It recomputes the frame digest and rejects stale or
superseded observations; it does not depend on legacy whole-universe deliveries.

## Budgets and operational controls

Hub and managed execution-controller requests share a PostgreSQL-atomic host
bucket, across accounts. Default total is 1800 weight/minute and market lane 900;
market traffic cannot consume the remaining management allowance. HTTP 429/418
applies shared cooldown, including private responses with malformed JSON.

Important boundary: the Web credential-verification process still uses its
existing independent read-only quota. A proposed extra table-write grant was
blocked by permission review and was NOT applied. The shared bucket must not be
described as an absolute limit on every process/IP consumer. Additional shared
integration needs a narrowly authorized quota interface; do not give the Web
role unrestricted budget-reset permissions.

Systemd bounds hub memory at 700 MiB and CPU at 120% of one core. The hub stops
collecting when free disk falls below the policy reserve (default 8 GiB). This
does not delete history. The account pipeline fails closed when hub heartbeat or
regime evidence becomes stale; position protection/recovery precedes that gate.
Process ownership uses a lifetime PG advisory lock; systemd restarts failures.

Policies are stored through versioned BusinessState CAS, not environment secrets:

```sh
python -m services.v2_market_policy
python -m services.v2_market_policy --expected-version 0 --set-json '{"batch_symbols":16,"background_slots":4}'
```

The JSON is a complete policy with omitted fields taking documented defaults,
not a patch to current values. Read the current version before editing. Updates
carry immutable state history. No account ID or API key is required in this policy.

## Capacity model and acceptance

Binance documents all-symbol `/fapi/v1/ticker/24hr` at 40 request weight and
one-minute Kline requests below 100 bars at 1 weight; 1440 bars cost 10.
[Official market-data reference](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data).

At 524 symbols, legacy full-history polling would cost 5240 weight/minute and
roughly 97 MB/minute using the measured representative 1440-bar payload size.
The warm incremental model is approximately 524 + 80 ticker + 2 time + catalog
requests per minute, excluding retries, private traffic and catchup. Cold start
still needs 5240 history weight spread across budget windows; it is not a burst.
These are planning estimates, not measured full-market throughput guarantees.

Initial targeted isolated regression: 175 passed. Includes atomic cross-account
budget contention, archive/cache replay, per-symbol failure isolation, 600-symbol
fair scheduling, duplicate cross-source admission, expired TV and legacy mode.
All tests use temporary databases/caches or explicit fakes and never send orders.
Full regression and real-resource acceptance are recorded separately below when
completed. Profitability is not established by integration or throughput tests.

## Deployment / rollback

Apply `db/migrations/20260929_shared_market.sql` as the existing V2 schema owner;
apply `db/clickhouse_v2_incremental_market.sql` only to the isolated V2 database.
Freeze a Git release under `/opt/trade-engine-v2/releases/`, never run from `/tmp`.
Install the hub unit and point `/opt/trade-engine-v2/market-hub` at that release.
Keep `V2_CONTROLLER_ENABLE_ENTRIES=false`. Switch the controller to the same
release with `V2_SHARED_MARKET=true` before starting the hub so the old collector
cannot simultaneously publish the same market contexts.

After restart the account controller intentionally recovers in DRAINING. This
does not authorize restoring ACTIVE/opening, liquidating holdings, switching
credentials or writing new orders. Existing exits/protection retain their own
original permissions. Check heartbeat, benchmark freshness, per-symbol coverage,
request weights, error reasons, CPU/RSS and disk growth before entry acceptance.

Rollback: stop the new hub, restore the previous controller release symlink and
disable shared mode, restart only that controller. Keep the new schema/history;
no database deletion is needed. Retain the entry-disable setting throughout.

## Recorded acceptance — 2026-09-27, UTC+8

Deployed runtime release: `8c3ffc8-shared-market` under
`/opt/trade-engine-v2/releases/`; both the hub and execution-controller symlinks
point there. The hub is enabled at boot with automatic failure restart. Both
services were active with zero automatic restarts at final verification.
The controller remains DRAINING/RECOVERING_SOURCE after the controlled restart,
with `V2_CONTROLLER_ENABLE_ENTRIES=false`; no new opening authority was enabled.

QA evidence:

- Broad isolated V2 regression on the frozen initial feature candidate:
  **1764 passed in 798.13 seconds**. An earlier run had two compatibility-test
  failures; these were fixed and this repeat run was clean.
- Subsequent real-driver normalization and recent-mover scoring changes:
  **75 targeted tests passed**, including the new feature, risk reference and
  no-local-file data-core boundary. The full suite was not rerun a third time
  after these small changes; do not label that targeted run a full regression.
- Ruff and `git diff --check` passed for the changed implementation.
- A real CH read verified all 1440 bars of an archived history after correcting
  the FixedString bytes/string mismatch. No archived data was deleted to mask it.
- PostgreSQL confirms Web has **no UPDATE privilege** on the shared budget table;
  the rejected privilege expansion was not applied.

Real integration evidence:

- All **524 supported contracts** eventually completed at least one deep
  collection successfully. The four unsupported names remain explicitly exposed.
- Final hub snapshot reported **524/524 lightweight coverage**, **256 fresh deep
  contexts** and 268 waiting for a fresh deep refresh. These metrics are distinct:
  success once is not a guarantee of a current tradable context.
- At 16:30 real TV signals for NILUSDT, FILUSDT and ORDIUSDT reached the new
  consumer. Their immutable decisions contain `universe_admission.eligible=true`
  and `signal_fusion.version=shared-context-v1`. FIL and ORDI are outside the old
  fixed scan list. Their long-side outcomes were respectively `overextended`,
  `score_below_minimum`, and `entry_mode_unconfirmed`; no forced order was sent.
  Opposite-side consumers recorded expiry, not late entry.
- The running pipeline reported market CURRENT, regime PROJECTED, and protection,
  exits, settlement and followups CLEAR. Entry dispatch stayed disabled.

An eight-minute warmup sample collected 92 distinct hub batches: mean 4837.7 ms,
P95 6003 ms, max 6322 ms. Hub memory was approximately 63.6–66.0 MiB and CPU about
60.8% of one core (approximately 15.2% of this four-core host). This sample overlapped
isolated QA and cold warmup, and preceded the final recent-mover sorting release;
it is not a multi-day steady-state benchmark. A final-release snapshot after
warmup showed a 2930 ms batch and about 55.4 MiB memory, not a new percentile claim.

During the sample CH held approximately 627310 candle rows in eight active parts,
60.7 MB on disk; the PG database grew from about 3631 MiB to 3648 MiB. Most CH
growth here was one-time 24h bootstrap, not a measured daily retention rate.
Continuous per-minute deep computation for every contract was NOT achieved by
this serial worker. The accepted operating model is full lightweight discovery
plus prioritized/fair, freshness-gated deep computation. Increasing batch size
alone is not evidence that more CPU/network capacity exists.

Remaining boundary: Web verification retains its independent quota pending
approval/design of a minimally privileged shared-quota interface. Non-ASCII
contract execution remains unsupported. LIVE activation, profitability, and
multi-day stability are not established by this acceptance. Historical analysis
and simulated/testnet outcomes must not be represented as live returns.
