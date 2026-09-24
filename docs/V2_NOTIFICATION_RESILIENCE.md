# Notification resilience

Confirmed failure mode: minute-batched signals alternated between queue-lag and
clear observations, producing repeated alarm/recovery transitions. A safety-
blocked trading pipeline intentionally waits for reconciliation; this must not
be confused with an independent consumer outage.

- Versioned policy controls continuous lag confirmation and quiet recovery.
  Deploy with 60 seconds confirmation and 120 seconds recovery; old defaults
  remain zero for backwards-compatible replay. Timers persist in PostgreSQL.
- Pending signals behind protection/exit failure are shown as upstream safety
  waits. Order-progress and position-safety alerts remain active. This change
  does not clear orders or permit entries behind unresolved exposure.
- New trade-open and trade-close messages save confirmed Telegram message IDs
  in account-scoped PG state before projector acknowledgement. Pin requests run
  only in the independent watchdog, one per iteration, with bounded exponential
  retry and silent pin notifications. Pin failure never retries sendMessage.
- A confirmed stored message also prevents a resend after projector-ACK failure.
  Telegram has no sendMessage idempotency key: loss between external send and
  durable receipt still cannot be advertised as exactly-once delivery.
- Pin destination binds bot identity and chat; a changed destination blocks old
  pin tasks rather than pinning an unrelated message in a new chat.
- No historical message IDs were retained before this change. Automatic pinning
  applies to newly confirmed trade notifications, not fabricated backfills.
- Runtime Telegram read-only permission checks confirmed pin rights. No fake
  trade message was sent to test pinning.

An existing UNKNOWN close request remains a separate reconciliation incident.
The exchange not-found response alone is not sufficient evidence to erase it or
resubmit it. Existing native protection and safety gates must remain in place.
