"""Durable account-scoped transfer health, never a permission to trade.

No daemon enabled here. An authenticated composition root must supply the account,
policy and tenant-specific notification destination. Send success is at-least-once.
"""

from dataclasses import asdict, dataclass
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.capital_journal import CapitalJournal, timestamp
from v2_core.telegram import TelegramOperationalSink


@dataclass(frozen=True)
class TransferMonitorPolicy:
    scan_ms: int = 60000
    overdue_ms: int = 1800000
    confirm_ms: int = 120000
    recovery_ms: int = 180000
    reminder_ms: int = 3600000
    retry_ms: int = 60000

    def __post_init__(self):
        if any(
            type(v) is not int or not 1 <= v <= 604800000 for v in asdict(self).values()
        ):
            raise ValueError("bounded positive monitor durations required")
        if self.reminder_ms < self.scan_ms:
            raise ValueError("reminders cannot be faster than scans")


class CapitalTransferMonitor:
    def __init__(self, connect):
        self.connect = connect

    def scan(self, tenant_id, registry_id, *, now_ms, policy):
        tenant, registry = uid(tenant_id), uid(registry_id)
        timestamp(now_ms)
        if not isinstance(policy, TransferMonitorPolicy):
            raise TypeError("typed monitor policy required")
        with self.connect() as c:
            CapitalJournal._lock(c, tenant)
            CapitalJournal._account(c, tenant, registry)
            prior = c.execute(
                "SELECT version,payload FROM v2_capital_monitor_state WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
            old = prior[1] if prior else {}
            if now_ms < old.get("observed_at_ms", 0):
                raise ValueError("MONITOR_CLOCK_REGRESSION")
            if now_ms < old.get("next_scan_ms", 0) and old.get("policy") == asdict(
                policy
            ):
                return dict(old, skipped=True)
            count, oldest = c.execute(
                """SELECT count(*),min(i.occurred_at_ms) FROM v2_exchange_income i
                JOIN v2_tenant_accounts a ON (a.exchange,a.account_id,a.environment,a.product)=(i.exchange,i.account_id,i.environment,i.product)
                LEFT JOIN v2_capital_baselines b ON (b.tenant_id,b.registry_id,b.currency)=(a.tenant_id,a.registry_id,i.currency)
                LEFT JOIN v2_capital_all_income_receipts r ON r.income_id=i.income_id
                WHERE a.tenant_id=%s AND a.registry_id=%s AND i.income_type='TRANSFER'
                AND i.occurred_at_ms<=%s AND (b.through_ms IS NULL OR i.occurred_at_ms>b.through_ms)
                AND (r.journal_id IS NULL OR EXISTS (SELECT 1 FROM v2_capital_journals undo
                    WHERE undo.reverses IN (r.journal_id,r.related_journal_id,b.journal_id) AND undo.occurred_at_ms<=%s))""",
                (tenant, registry, now_ms - policy.overdue_ms, now_ms),
            ).fetchone()
            active = old.get("active", False)
            first = old.get("first_seen_ms") if count else None
            if count and first is None:
                first = now_ms
            clear = old.get("clear_since_ms") if not count and active else None
            if not count and active and clear is None:
                clear = now_ms
            kind = None
            if count and not active and now_ms - first >= policy.confirm_ms:
                active, kind = True, "OPEN"
            elif (
                count
                and active
                and now_ms - old["last_emitted_ms"] >= policy.reminder_ms
            ):
                kind = "REMINDER"
            elif not count and active and now_ms - clear >= policy.recovery_ms:
                active, kind = False, "RECOVERED"
            result = {
                "observed_at_ms": now_ms,
                "next_scan_ms": now_ms + policy.scan_ms,
                "active": active,
                "first_seen_ms": first,
                "clear_since_ms": clear,
                "last_emitted_ms": now_ms if kind else old.get("last_emitted_ms"),
                "overdue_count": count,
                "oldest_event_ms": oldest,
                "policy": asdict(policy),
                "execution_authorized": False,
                "coverage_status": "NOT_PROVEN",
            }
            c.execute(
                "INSERT INTO v2_capital_monitor_state(tenant_id,registry_id,version,payload) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,registry_id) DO UPDATE SET version=EXCLUDED.version,payload=EXCLUDED.payload",
                (tenant, registry, prior[0] + 1 if prior else 1, Jsonb(result)),
            )
            if kind:
                c.execute(
                    "INSERT INTO v2_capital_monitor_events(event_id,tenant_id,registry_id,kind,payload) VALUES (%s,%s,%s,%s,%s)",
                    (str(uuid4()), tenant, registry, kind, Jsonb(result)),
                )
            return dict(result, emitted=kind, skipped=False)

    def deliver_latest(
        self, tenant_id, registry_id, *, destination_ref, now_ms, policy, send
    ):
        """Coalesce stale queued states. Callback must be bound to this destination.

        I/O holds a delivery-only transaction lock, never the capital scan lock.
        Transport MUST have a timeout. Crash after send before commit may duplicate.
        """
        tenant, registry, destination = (
            uid(tenant_id),
            uid(registry_id),
            uid(destination_ref),
        )
        timestamp(now_ms)
        if not isinstance(policy, TransferMonitorPolicy) or not callable(send):
            raise TypeError("explicit bounded delivery policy and sender required")
        with self.connect() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("capital-monitor-delivery:" + tenant + ":" + registry,),
            )
            CapitalJournal._account(c, tenant, registry)
            c.execute(
                "INSERT INTO v2_capital_monitor_delivery(tenant_id,registry_id,destination_ref) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant, registry, destination),
            )
            saved = c.execute(
                "SELECT destination_ref::text,last_sequence,next_attempt_ms,last_attempt_ms FROM v2_capital_monitor_delivery WHERE tenant_id=%s AND registry_id=%s",
                (tenant, registry),
            ).fetchone()
            if saved[0] != destination:
                raise ValueError("MONITOR_DESTINATION_CONFLICT")
            if now_ms < saved[3]:
                raise ValueError("MONITOR_CLOCK_REGRESSION")
            if now_ms < saved[2]:
                return "BACKOFF"
            event = c.execute(
                "SELECT sequence,event_id::text,kind,payload FROM v2_capital_monitor_events WHERE tenant_id=%s AND registry_id=%s AND sequence>%s ORDER BY sequence DESC LIMIT 1",
                (tenant, registry, saved[1]),
            ).fetchone()
            if event is None:
                return "IDLE"
            try:
                confirmed = (
                    send(
                        {
                            "event_id": event[1],
                            "tenant_id": tenant,
                            "registry_id": registry,
                            "destination_ref": destination,
                            "kind": event[2],
                            "payload": event[3],
                        }
                    )
                    is True
                )
            except Exception:  # noqa: BLE001 - never persist transport secrets or exception text
                confirmed = False
            c.execute(
                "UPDATE v2_capital_monitor_delivery SET last_sequence=%s,next_attempt_ms=%s,last_attempt_ms=%s WHERE tenant_id=%s AND registry_id=%s",
                (
                    event[0] if confirmed else saved[1],
                    now_ms + policy.retry_ms,
                    now_ms,
                    tenant,
                    registry,
                ),
            )
            return "SENT" if confirmed else "RETRY"


class CapitalMonitorTelegram:
    """Explicit internal route binding; not authentication or chat ownership proof."""

    def __init__(self, connect, *, tenant_id, registry_id, destination_ref, sink):
        if not isinstance(sink, TelegramOperationalSink):
            raise TypeError("bounded Telegram operational transport required")
        self.connect, self.sink = connect, sink
        self.tenant, self.registry, self.destination = map(
            uid, (tenant_id, registry_id, destination_ref)
        )

    def __call__(self, event):
        if (
            event.get("tenant_id"),
            event.get("registry_id"),
            event.get("destination_ref"),
        ) != (self.tenant, self.registry, self.destination):
            raise ValueError("MONITOR_ROUTE_MISMATCH")
        if event.get("kind") not in {"OPEN", "REMINDER", "RECOVERED"}:
            raise ValueError("MONITOR_EVENT_INVALID")
        with self.connect() as c:
            row = c.execute(
                "SELECT account_id,environment FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
                (self.tenant, self.registry),
            ).fetchone()
        if row is None or row[1] != self.sink.environment:
            raise ValueError("MONITOR_ROUTE_MISMATCH")
        return self.sink(
            event["event_id"],
            row[1],
            "TRADING_HEALTH",
            {
                "account_id": row[0],
                "error_code": "CAPITAL_TRANSFER_OVERDUE",
                "status": "RECOVERED"
                if event["kind"] == "RECOVERED"
                else "UNAVAILABLE",
                "observed_at_ms": event["payload"]["observed_at_ms"],
            },
        )
