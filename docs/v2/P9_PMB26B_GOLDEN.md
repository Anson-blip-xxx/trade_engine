# Phase 9-08A - PMB-26B Telegram Failure Semantics

> Characterization only; production diff is zero.

## Observed Call Order

`PositionReconcileService.notify_external_position` performs the alert path in
this order after the 30-second grace period:

1. Check the 24-hour seen fingerprint.
2. Write the seen fingerprint and timestamp.
3. Clear the pending key.
4. Write the PM log entry.
5. Attempt the Telegram request inside `try/except Exception: pass`.
6. Record the PG event outside that exception handler.

## Failure Matrix

| Condition | Seen written | Pending cleared | PG called | Caller sees error |
|---|---:|---:|---:|---:|
| TG succeeds | yes | yes | yes | no |
| TG returns `None`/`False` | yes | yes | yes | no |
| TG raises | yes | yes | yes | no |
| PG raises after TG | yes | yes | attempted | **yes** |

Telegram is therefore a best-effort side channel. Its result is not an
acknowledgement and its failure does not block logging, PG recording, another
symbol, or the calling reconcile flow.

## Pending And Seen Semantics

- The first observation writes only the pending fingerprint and timestamp.
- The same fingerprint at exactly 30 seconds passes the strict `< 30` guard.
- Seen is written before Telegram is attempted.
- After a failed Telegram attempt, another observation starts a new pending
  window, but the same fingerprint is suppressed by seen for 24 hours.
- Once seen is at least 24 hours old, another delivery attempt is allowed.

## Risk

The swallowed exception preserves reconcile availability, but a transient
Telegram outage can hide that notification for 24 hours because failed
delivery is marked seen before transport success is known. Changing this would
require a product decision about retries, duplicate alerts, acknowledgement,
and whether PG success is the durable notification source.

## Conclusion

**PMB-26B = KEEP AS-IS / INTENTIONAL BEST-EFFORT / CLOSED.**

No production change is justified for the exception boundary itself. Track
delivery acknowledgement and retry policy separately as **PMB-26B2 - NEEDS
PRODUCT DECISION**. PMB-26C remains the separate PG propagation ticket.
