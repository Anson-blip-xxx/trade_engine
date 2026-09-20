"""Durable unresolved-order alerts. Never substitutes timeout for exchange fact."""

from v2_core.ledger import emit, lock_order_episode
from v2_core.scoping import predicate, validate_scope


class RecoveryAttention:
    def __init__(self, connection_factory, *, scope=None):
        self._connect = connection_factory
        self.scope = validate_scope(scope)

    def scan(self, *, now_ms, overdue_ms, limit=100):
        if (
            type(now_ms) is not int
            or now_ms < 0
            or type(overdue_ms) is not int
            or overdue_ms <= 0
        ):
            raise ValueError(
                "explicit timestamp and positive overdue duration required"
            )
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        condition, params = predicate(self.scope)
        with self._connect() as conn:
            candidates = conn.execute(
                f"""SELECT o.order_id::text FROM v2_orders o
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE {condition} AND o.status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')
                  AND extract(epoch FROM updated_at)*1000 <= %s
                  AND NOT EXISTS (SELECT 1 FROM v2_domain_outbox e
                    WHERE e.intent_id=o.episode_id AND e.event_type=
                      'ATTENTION_REQUIRED:' || o.order_id::text || ':' || o.version::text)
                ORDER BY updated_at,order_id LIMIT %s""",
                (*params, now_ms - overdue_ms, limit),
            ).fetchall()
        count = 0
        for (order_id,) in candidates:
            with self._connect() as conn:
                lock_order_episode(conn, order_id)
                row = conn.execute(
                    """SELECT episode_id::text,status,version,client_order_id,
                    extract(epoch FROM updated_at)*1000 FROM v2_orders WHERE order_id=%s""",
                    (order_id,),
                ).fetchone()
                if (
                    row[1] not in {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"}
                    or row[4] > now_ms - overdue_ms
                ):
                    continue
                kind = f"ATTENTION_REQUIRED:{order_id}:{row[2]}"
                if conn.execute(
                    "SELECT 1 FROM v2_domain_outbox WHERE intent_id=%s AND event_type=%s",
                    (row[0], kind),
                ).fetchone():
                    continue
                emit(
                    conn,
                    row[0],
                    kind,
                    {
                        "order_id": order_id,
                        "client_order_id": row[3],
                        "order_version": row[2],
                        "status": row[1],
                        "observed_at_ms": now_ms,
                        "fallback": "QUERY_ONLY",
                        "reason": "exchange outcome requires reconciliation",
                    },
                )
                count += 1
        return count


class AttentionNotifications:
    """Inject a TG-compatible notification port; no credentials or HTTP here.

    Delivery is at least once: the event ID is visible for operator deduplication.
    A TG acknowledgement loss can produce duplicates; there is no exactly-once
    promise. Notifications never carry executable approval commands.
    """

    def __init__(self, connection_factory, send):
        if not callable(send):
            raise TypeError("explicit notification port required")
        self._connect, self.send = connection_factory, send

    def __call__(self, event_id, intent_id, kind, payload):
        if not kind.startswith("ATTENTION_REQUIRED:"):
            return True
        with self._connect() as conn:
            current = conn.execute(
                "SELECT status,version FROM v2_orders WHERE order_id=%s",
                (payload["order_id"],),
            ).fetchone()
        if current != (payload["status"], payload["order_version"]):
            return True  # Resolved/superseded event stays in audit, not a new alert.
        message = (
            f"V2 订单待核验\n事件：{event_id}\n意图：{intent_id}\n"
            f"订单：{payload['client_order_id']}\n状态：{payload['status']}\n"
            "自动处理：继续原订单身份查询，不重复下单。通知不是当前场景的交易授权。"
        )
        return self.send(message) is True
