"""Durable, freshness-aware strategy scheduling; never dispatches an order.

Leases reduce duplicate pure evaluation, not guarantee exactly-once execution.
StrategyWorker's immutable decision and idempotent admission are the authority.
"""

import re
from copy import deepcopy
from uuid import uuid4

EXPECTED_ADMISSION_WAITS = frozenset(
    {
        "ACCOUNT_RISK_CAPACITY_UNAVAILABLE",
        "EXISTING_SYMBOL_POSITION",
    }
)


def diagnostic_code(exc):
    detail = str(exc)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", detail):
        return detail
    return type(exc).__name__


class StrategyScheduler:
    def __init__(self, worker, *, context_provider, context_required=None):
        context_required = (
            (lambda _: True) if context_required is None else context_required
        )
        if not callable(context_provider) or not callable(context_required):
            raise TypeError("explicit read-only context policy required")
        self.worker, self.context_provider, self.context_required = (
            worker,
            context_provider,
            context_required,
        )
        self._connect = worker._connect

    def expire_pending(self, *, limit=1000):
        """Retire never-decided expired signals in one bounded transaction.

        The original inbox and immutable receipt remain the audit. Signal locks
        serialize with StrategyWorker's first-decision commit. Recheck after
        acquiring locks so a concurrent committed decision is never discarded.
        Existing decisions/intents and active leases keep their recovery path.
        """
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("bounded expiry batch required")
        worker, consumer, now = (
            self.worker,
            self.worker.scope.consumer,
            self.worker.runtime._now(),
        )
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT s.signal_id FROM v2_inbound_signals s
                WHERE s.source=%s AND s.environment=%s
                AND (s.snapshot->>'expires_at_ms')::bigint<=%s
                AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                    WHERE r.consumer=%s AND r.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_decisions d
                    WHERE d.consumer=%s AND d.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_trade_intents i WHERE i.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id
                    AND t.lease_until>=clock_timestamp())
                ORDER BY s.received_at,s.signal_id LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
                (
                    worker.source,
                    worker.scope.environment,
                    now,
                    consumer,
                    consumer,
                    consumer,
                    limit,
                ),
            ).fetchall()
            if not rows:
                return 0
            expired = conn.execute(
                """INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason)
                SELECT %s,s.signal_id,'EXPIRED','signal expired before scheduling'
                FROM v2_inbound_signals s WHERE s.signal_id=ANY(%s::uuid[])
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_decisions d
                    WHERE d.consumer=%s AND d.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_trade_intents i WHERE i.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id
                    AND t.lease_until>=clock_timestamp())
                ON CONFLICT DO NOTHING RETURNING signal_id""",
                (consumer, [row[0] for row in rows], consumer, consumer),
            ).fetchall()
            if expired:
                conn.execute(
                    """INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at)
                    SELECT %s,unnest(%s::uuid[]),clock_timestamp()
                    ON CONFLICT(consumer,signal_id) DO UPDATE SET
                        completed_at=EXCLUDED.completed_at,error_code=NULL""",
                    (consumer, [row[0] for row in expired]),
                )
        return len(expired)

    def progress(self):
        """Return an atomic source-to-consumer catch-up snapshot."""
        worker, consumer = self.worker, self.worker.scope.consumer
        with self._connect() as conn:
            row = conn.execute(
                """WITH scoped_signals AS (
                    SELECT signal_id FROM v2_inbound_signals
                    WHERE source=%s AND environment=%s),
                totals AS (
                    SELECT count(*) AS source_signals,
                    count(t.signal_id) AS scheduled,
                    count(t.signal_id) FILTER (WHERE t.completed_at IS NOT NULL) AS completed,
                    count(t.signal_id) FILTER (WHERE t.completed_at IS NULL) AS incomplete
                    FROM scoped_signals s LEFT JOIN v2_strategy_tasks t
                    ON t.consumer=%s AND t.signal_id=s.signal_id)
                SELECT source_signals,scheduled,completed,incomplete,
                source_signals-scheduled AS undiscovered FROM totals""",
                (worker.source, worker.scope.environment, consumer),
            ).fetchone()
        source, scheduled, completed, incomplete, undiscovered = map(int, row)
        return {
            "source_signals": source,
            "scheduled": scheduled,
            "completed": completed,
            "incomplete": incomplete,
            "undiscovered": undiscovered,
            "caught_up": undiscovered == 0 and incomplete == 0,
        }

    def run_once(self, limit=100, *, interval_seconds=5, lease_seconds=300):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid strategy batch limit")
        if any(
            type(v) is not int or not 1 <= v <= 3600
            for v in (interval_seconds, lease_seconds)
        ):
            raise ValueError("bounded strategy cadence and lease required")
        worker, consumer = self.worker, self.worker.scope.consumer
        now = worker.runtime._now()
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
                ORDER BY CASE
                    WHEN EXISTS (SELECT 1 FROM v2_strategy_decisions d
                        WHERE d.consumer=%s AND d.signal_id=s.signal_id) THEN 0
                    WHEN (s.snapshot->>'observed_at')::bigint<=%s
                        AND (s.snapshot->>'expires_at_ms')::bigint>%s THEN 1
                    ELSE 2 END,s.received_at,s.signal_id
                LIMIT %s ON CONFLICT DO NOTHING""",
                (
                    consumer,
                    worker.source,
                    worker.scope.environment,
                    consumer,
                    consumer,
                    now,
                    now,
                    limit,
                ),
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
                    ORDER BY CASE
                        WHEN EXISTS (SELECT 1 FROM v2_strategy_decisions d
                            WHERE d.consumer=t.consumer AND d.signal_id=t.signal_id) THEN 0
                        WHEN (s.snapshot->>'observed_at')::bigint<=%s
                            AND (s.snapshot->>'expires_at_ms')::bigint>%s THEN 1
                        ELSE 2 END,t.next_attempt_at,t.signal_id LIMIT 1
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
                        worker.runtime._now(),
                        worker.runtime._now(),
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
                eligible_for_context = (
                    stored is None
                    and receipt is None
                    and snapshot["signal"] != "EVENT_END"
                    and snapshot["observed_at"] <= now < snapshot["expires_at_ms"]
                )
                needs_context = False
                if eligible_for_context:
                    needs_context = self.context_required(deepcopy(snapshot))
                    if type(needs_context) is not bool:
                        raise TypeError("context policy must return bool")
                context = (
                    self.context_provider(deepcopy(snapshot)) if needs_context else {}
                )
                results[signal_id] = worker.consume(signal_id, context=context)[
                    "status"
                ]
            except Exception as exc:  # noqa: BLE001 - poison signals cannot stop other tasks
                error = diagnostic_code(exc)
                # Explicit capacity/position gates are expected waits, not a
                # dependency failure that blocks unrelated prepared orders.
                results[signal_id] = (
                    "DEFERRED"
                    if isinstance(exc, ValueError) and error in EXPECTED_ADMISSION_WAITS
                    else "UNAVAILABLE"
                )
                if error in EXPECTED_ADMISSION_WAITS and not isinstance(
                    exc, ValueError
                ):
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
