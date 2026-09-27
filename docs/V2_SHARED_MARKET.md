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
