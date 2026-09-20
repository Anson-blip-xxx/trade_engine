# V2 Operator Decision Inbox And Notification Outbox Foundation

> **IMPLEMENTED / CLOSED as dormant infrastructure.** The standalone schema was
> not applied to any persistent or production database. An injected PostgreSQL
> adapter is implemented and tested only against disposable QA databases. There
> is no DSN lookup, driver import, Telegram transport, HTTP route, frontend,
> scheduler, worker, or runtime import.

## Durable authorities

`operator_decisions` owns the durable Web inbox item: immutable event/slot
identity, safe trigger/current snapshots and digests, severity, fallback action
and deadline, approval expiry, assignee, resolution, and monotonic version.

`operator_notification_outbox` owns notification delivery state. A decision and
its initial Telegram outbox item can later be inserted in one PostgreSQL
transaction. The channel is currently restricted to Telegram and each item has
a unique dedupe key, version, attempt count, due time, owner lease, delivery
time, and dead-letter state.

The Web inbox is not trading authority, and Telegram delivery is not business
acknowledgement. Snapshot payloads are explicitly safe summaries, never raw
credentials, signed requests, or exchange secrets.

## Injected PostgreSQL adapter

`PostgresOperatorDecisionStore` receives a transaction-scoped connection
factory. It atomically creates a decision and its initial notification, exposes
version-fenced decision CAS, lists only active inbox rows, and claims bounded
notification batches with `FOR UPDATE SKIP LOCKED`. Notification completion is
fenced by version, owner token, and a live database-clock lease. Every ambiguous
write/commit failure returns an explicit `UNKNOWN` outcome.

## State rules

- decision status is `OPEN -> ACKNOWLEDGED -> RESOLVED`, with direct
  `EXPIRED`/`SUPERSEDED` terminal alternatives;
- only `RESOLVED` carries a typed resolution and detail;
- current snapshot refresh is versioned and forbidden after terminal state;
- notification flow is `PENDING/RETRY_WAIT -> CLAIMED ->
  DELIVERED/RETRY_WAIT/DEAD_LETTER`;
- claim requires the item to be due and creates a live owner lease;
- a live lease rejects takeover; an expired claim can be reclaimed after a
  worker crash;
- retry requires an error and future due time;
- delivery and dead-letter states are terminal in the domain model.

## Remaining slices

1. read-only authenticated API and UI;
2. Telegram renderer/transport with safe deep links;
3. approval audit/signing and default-off executor integration.

**V2-OPERATOR-DECISION-FOUNDATION PASS.**
