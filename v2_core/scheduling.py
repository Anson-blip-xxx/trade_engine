"""Durable, fair strategy admission scheduling; never dispatches an order.

Leases reduce duplicate pure evaluation, not guarantee exactly-once execution.
StrategyWorker's immutable decision and idempotent admission are the authority.
"""

from copy import deepcopy
from uuid import uuid4


class StrategyScheduler:
    def __init__(self, worker, *, context_provider):
        if not callable(context_provider):
            raise TypeError("explicit read-only context provider required")
        self.worker, self.context_provider = worker, context_provider
        self._connect = worker._connect

    def run_once(self, limit=100, *, interval_seconds=5, lease_seconds=300):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid strategy batch limit")
        if any(
            type(v) is not int or not 1 <= v <= 3600
            for v in (interval_seconds, lease_seconds)
        ):
            raise ValueError("bounded strategy cadence and lease required")
        worker, consumer = self.worker, self.worker.scope.consumer
        # No high-water mark: late commits and previously admitted-but-unprepared
        # intents remain discoverable. Previously scheduled tasks never starve
        # discovery of later signals, including when earlier tasks are failing.
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO v2_strategy_tasks(consumer,signal_id)
                SELECT %s,s.signal_id FROM v2_inbound_signals s
                WHERE s.source=%s AND s.environment=%s
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id)
                ORDER BY s.received_at,s.signal_id LIMIT %s ON CONFLICT DO NOTHING""",
                (consumer, worker.source, worker.scope.environment, consumer, limit),
            )
        results = {}
        for _ in range(limit):
            token = str(uuid4())
            # Claim just before execution, not an entire batch whose leases can
            # expire while preceding callbacks are running.
            with self._connect() as conn:
                row = conn.execute(
                    """WITH due AS (
                    SELECT t.signal_id FROM v2_strategy_tasks t
                    JOIN v2_inbound_signals s USING(signal_id)
                    WHERE t.consumer=%s AND s.source=%s AND s.environment=%s
                    AND t.completed_at IS NULL AND t.next_attempt_at<=clock_timestamp()
                    AND (t.lease_until IS NULL OR t.lease_until<clock_timestamp())
                    AND NOT (t.signal_id=ANY(%s::uuid[]))
                    ORDER BY t.next_attempt_at,t.signal_id LIMIT 1
                    FOR UPDATE OF t SKIP LOCKED)
                    UPDATE v2_strategy_tasks t SET attempts=attempts+1,lease_token=%s,
                    lease_until=clock_timestamp()+%s*interval '1 second'
                    FROM due WHERE t.consumer=%s AND t.signal_id=due.signal_id
                    RETURNING t.signal_id::text,t.attempts""",
                    (
                        consumer,
                        worker.source,
                        worker.scope.environment,
                        list(results),
                        token,
                        lease_seconds,
                        consumer,
                    ),
                ).fetchone()
            if row is None:
                break
            signal_id, attempt = row
            error = None
            try:
                with self._connect() as conn:
                    snapshot, stored, receipt = worker._read(conn, signal_id)
                now = worker.runtime._now()
                # Recovery must not depend on a fresh market snapshot. In
                # particular an unavailable feed cannot prevent expiry handling.
                needs_context = (
                    stored is None
                    and receipt is None
                    and snapshot["signal"] != "EVENT_END"
                    and snapshot["observed_at"] <= now < snapshot["expires_at_ms"]
                )
                context = (
                    self.context_provider(deepcopy(snapshot)) if needs_context else {}
                )
                results[signal_id] = worker.consume(signal_id, context=context)[
                    "status"
                ]
            except Exception as exc:  # noqa: BLE001 - poison signals cannot stop other tasks
                results[signal_id] = "UNAVAILABLE"
                error = type(exc).__name__
            delay = (
                interval_seconds
                if error is None
                else max(interval_seconds, min(300, 2 ** min(attempt, 9)))
            )
            try:
                with self._connect() as conn:
                    # Derive completion from committed business records, never
                    # a callback's return value. INTENT alone is insufficient.
                    conn.execute(
                        """UPDATE v2_strategy_tasks t SET
                        completed_at=CASE WHEN EXISTS (
                            SELECT 1 FROM v2_signal_receipts r
                            LEFT JOIN v2_trade_intents i ON i.intent_id=r.intent_id
                            WHERE r.consumer=t.consumer AND r.signal_id=t.signal_id
                            AND (r.outcome<>'INTENT' OR i.status<>'RECEIVED'
                                OR EXISTS (SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id))
                        ) THEN clock_timestamp() ELSE NULL END,
                        next_attempt_at=clock_timestamp()+%s*interval '1 second',
                        lease_token=NULL,lease_until=NULL,error_code=%s
                        WHERE t.consumer=%s AND t.signal_id=%s AND t.lease_token=%s""",
                        (delay, error, consumer, signal_id, token),
                    )
            except Exception:  # noqa: BLE001 - lease expiry retries lost scheduling acknowledgement
                results[signal_id] = "UNAVAILABLE"
        return results
