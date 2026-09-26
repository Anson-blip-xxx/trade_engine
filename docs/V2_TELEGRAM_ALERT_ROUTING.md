# Telegram alert separation and market recovery

Set `V2_TG_ALERT_CHAT_ID` in the protected runtime environment to the private user
chat ID. Never commit the real ID, bot token, runtime snapshots or balances.
The existing `TG_NOTIFY_CHAT_ID` remains the trade destination. Daemon, watchdog
and standalone market/inventory/protection entry points accept this setting.
Standalone commands must explicitly inherit the configured environment.

With this setting, only TRADE_OPENED/TRADE_CLOSED go to the trade destination;
all other supported operational events go to DM. Trade pinning stays attached
to the trade destination. DM rejection retains existing delivery retry semantics
and never falls back to the group. Telegram requires the user to start the bot;
verify a test DM before enabling. An unset setting preserves legacy routing.

Collection failures preserve only a whitelist of public-market diagnostic codes,
not raw exception text, URLs, headers or API responses. The notification reports
incident start, observation time (UTC+8) and elapsed duration at that observation.
Transport failures still group network/timeout/parse errors; no speculative cause.
Repeated failures remain coalesced in five-minute buckets. Incident history is
durable in the existing operational outbox, serialized by environment lock.
No process-memory or local-file incident state is introduced.
Pre-upgrade failure records lack incident boundaries and are excluded from duration
and recovery reconstruction; historical failures must not imply continuous outage.

A MARKET_RECOVERED event is emitted once after a newly collected batch is
ACKNOWLEDGED. Idle, pending or retry states do not count as recovery. Recovery
does not prove order/execution health. Notifications are at-least-once and a
delivery backlog may delay messages; use the displayed observation timestamp.

Before deployment apply `db/migrations/20260926_market_recovery.sql` transactionally
with a lock timeout. This extends only operational event types and their index;
it does not apply SaaS migrations or change financial state. Old binaries can run
with the expanded constraint but will not deliver the new recovery event type.
Roll back the service code if needed; do not delete recorded events.

Deployment is a minimal overlay on the existing Testnet release, not a rollout
of all V2 SaaS changes. No leverage, position, risk or strategy policy is changed.

QA: 179 tests passed across routing/recovery, operational delivery, market pipeline,
daemon entry/bootstrap, watchdog, inventory, trading health, capital monitor and
trade notifications. Tests cover failed DM without group fallback, preserved trade
pin destination, restart-safe recovery, concurrent coalescing, reopening within a
bucket and no recovery from idle/retry. No real user ID is in these new changes.
