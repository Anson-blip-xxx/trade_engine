"""At-least-once projection delivery with per-event durable receipts.

The sink MUST implement idempotent event-ID storage. Delivery can succeed before
a receipt commits; replay is expected. No sequential-ID high-water mark is used:
transactions may commit in a different order from sequence allocation.
"""

from uuid import uuid4


class Projector:
    def __init__(self, connection_factory, consumer, sink):
        if not isinstance(consumer, str) or not consumer.strip():
            raise ValueError("consumer identity required")
        if not callable(sink):
            raise TypeError("sink must be callable")
        self._connect, self.consumer, self.sink = connection_factory, consumer, sink

    def run_batch(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        with self._connect() as conn:
            events = conn.execute(
                """SELECT e.event_id::text,e.intent_id::text,
                e.event_type,e.payload FROM v2_domain_outbox e
                WHERE NOT EXISTS (SELECT 1 FROM v2_consumer_receipts r
                    WHERE r.consumer=%s AND r.event_id=e.event_id)
                ORDER BY e.created_at,e.event_id LIMIT %s""",
                (self.consumer, limit),
            ).fetchall()
        for event_id, intent_id, kind, payload in events:
            # Explicit False is failure; exceptions also leave the item pending.
            if self.sink(event_id, intent_id, kind, payload) is not True:
                raise RuntimeError("projection was not acknowledged")
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO v2_consumer_receipts(consumer,event_id)
                    VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                    (self.consumer, event_id),
                )
        return len(events)

    def run_scheduled_batch(self, limit=100, *, lease_seconds=60):
        """Bounded retrying worker; one bad event cannot block later events.

        Lease tokens fence receipt writes, not external I/O. A slow worker may
        overlap a replacement: sinks still MUST deduplicate immutable event IDs.
        Sink calls must have a finite transport timeout. No automatic discard.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        token = str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO v2_delivery_attempts(consumer,event_id)
                SELECT %s,e.event_id FROM v2_domain_outbox e
                WHERE NOT EXISTS (SELECT 1 FROM v2_consumer_receipts r
                    WHERE r.consumer=%s AND r.event_id=e.event_id)
                  AND NOT EXISTS (SELECT 1 FROM v2_delivery_attempts a
                    WHERE a.consumer=%s AND a.event_id=e.event_id)
                ORDER BY e.created_at,e.event_id LIMIT %s ON CONFLICT DO NOTHING""",
                (self.consumer, self.consumer, self.consumer, limit),
            )
            events = conn.execute(
                """WITH due AS (
                    SELECT a.event_id FROM v2_delivery_attempts a
                    WHERE a.consumer=%s AND a.next_attempt_at<=clock_timestamp()
                      AND (a.lease_until IS NULL OR a.lease_until<clock_timestamp())
                      AND NOT EXISTS (SELECT 1 FROM v2_consumer_receipts r
                        WHERE r.consumer=a.consumer AND r.event_id=a.event_id)
                    ORDER BY a.next_attempt_at,a.event_id LIMIT %s
                    FOR UPDATE OF a SKIP LOCKED
                ), claimed AS (
                    UPDATE v2_delivery_attempts a SET attempts=attempts+1,
                        lease_token=%s,lease_until=clock_timestamp()+%s*interval '1 second'
                    FROM due WHERE a.consumer=%s AND a.event_id=due.event_id
                    RETURNING a.event_id,a.attempts
                ) SELECT e.event_id::text,e.intent_id::text,e.event_type,e.payload,c.attempts
                FROM claimed c JOIN v2_domain_outbox e USING(event_id)""",
                (self.consumer, limit, token, lease_seconds, self.consumer),
            ).fetchall()
        result = {"claimed": len(events), "delivered": 0, "failed": 0, "superseded": 0}
        for event_id, intent_id, kind, payload, attempt in events:
            failure = None
            try:
                if self.sink(event_id, intent_id, kind, payload) is not True:
                    raise RuntimeError("projection was not acknowledged")
            except Exception as exc:  # noqa: BLE001 - durable bounded retries
                # Do not persist exception strings: they can contain DSNs/secrets.
                failure = type(exc).__name__
            with self._connect() as conn:
                owned = conn.execute(
                    """SELECT 1 FROM v2_delivery_attempts
                    WHERE consumer=%s AND event_id=%s AND lease_token=%s FOR UPDATE""",
                    (self.consumer, event_id, token),
                ).fetchone()
                if owned is None:
                    result["superseded"] += 1
                    continue
                if failure is None:
                    conn.execute(
                        """INSERT INTO v2_consumer_receipts(consumer,event_id)
                        VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                        (self.consumer, event_id),
                    )
                    result["delivered"] += 1
                else:
                    result["failed"] += 1
                conn.execute(
                    """UPDATE v2_delivery_attempts SET lease_until=NULL,lease_token=NULL,
                    error_code=%s,next_attempt_at=clock_timestamp()+%s*interval '1 second'
                    WHERE consumer=%s AND event_id=%s""",
                    (failure, min(300, 2 ** min(attempt, 9)), self.consumer, event_id),
                )
        return result
