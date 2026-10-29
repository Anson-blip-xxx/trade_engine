"""Durable, freshness-aware strategy scheduling; never dispatches an order.

Leases reduce duplicate pure evaluation, not guarantee exactly-once execution.
StrategyWorker's immutable decision and idempotent admission are the authority.
"""

import re
import time
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

EXPECTED_ADMISSION_WAITS = frozenset(
    {
        "ACCOUNT_RISK_CAPACITY_UNAVAILABLE",
        "ADMISSION_BUDGET_UNAVAILABLE",
        "EXISTING_SYMBOL_POSITION",
    }
)
EXPECTED_CONTEXT_WAITS = frozenset({"STALE_CONTEXT", "SIGNAL_EXPIRED_DURING_CONTEXT"})

# These are data-acquisition failures of the one candidate being assembled,
# not evidence that the account or execution dependencies are unsafe. Keep an
# explicit contract: neither name prefixes, arbitrary ValueErrors nor transport
# exceptions with matching text may grant this disposition. New validations
# must deliberately choose their failure scope instead of silently inheriting
# a broad "ignore market errors" rule.
CANDIDATE_CONTEXT_WAITS = {
    "SHORT_RATIO_MISSING": "SYMBOL_DATA_MISSING",
    "SHORT_RATIO_STALE": "SYMBOL_DATA_EXPIRED",
    "ENTRY_RESEARCH_CONTEXT_EXPIRED": "CANDIDATE_CONTEXT_EXPIRED",
    "ENTRY_STRUCTURE_CONTEXT_EXPIRED": "CANDIDATE_CONTEXT_EXPIRED",
}
TERMINAL_CONTEXT_FAILURES = frozenset({"SHORT_RATIO_MISSING", "SHORT_RATIO_STALE"})


def candidate_wait_reason(exc, *, stage):
    """Classify a known pre-consumption wait, never authorize an order.

    Existing admission capacity waits keep their historical contract. New
    symbol/research failures require the precise exception type currently
    raised by their validators and may only occur while gathering context.
    Scope, timestamp-shape, authentication, accounting and persistence errors
    intentionally do not belong to this contract.
    """
    if not isinstance(exc, ValueError):
        return None
    code = diagnostic_code(exc)
    if code in EXPECTED_ADMISSION_WAITS:
        return "ADMISSION_CAPACITY_WAIT"
    if stage != "CONTEXT":
        return None
    if code in EXPECTED_CONTEXT_WAITS:
        return "CANDIDATE_CONTEXT_EXPIRED"
    return CANDIDATE_CONTEXT_WAITS.get(code) if type(exc) is ValueError else None


def diagnostic_code(exc):
    detail = str(exc)
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", detail):
        return detail
    return type(exc).__name__


def drain_requested(check):
    value = False if check is None else check()
    if type(value) is not bool:
        raise TypeError("stop check must return bool")
    return value


class _DrainAttempt(Exception):
    """Cooperative yield before business evaluation, never an exchange cancel."""


class StrategyScheduler:
    def __init__(
        self,
        worker,
        *,
        context_provider,
        context_required=None,
        priority_events=(),
        actionable_events=None,
        monotonic=time.monotonic,
        context_failure_attempts=2,
    ):
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
        if not isinstance(priority_events, tuple) or any(
            not isinstance(v, str) or not v for v in priority_events
        ):
            raise ValueError("explicit priority event types required")
        self.priority_events, self.monotonic = priority_events, monotonic
        if actionable_events is not None and (
            not isinstance(actionable_events, tuple)
            or not actionable_events
            or any(
                not isinstance(value, str) or not value for value in actionable_events
            )
        ):
            raise ValueError("explicit actionable event contract required")
        self.actionable_events = actionable_events
        if (
            type(context_failure_attempts) is not int
            or not 2 <= context_failure_attempts <= 20
        ):
            raise ValueError("bounded candidate context attempts required")
        self.context_failure_attempts = context_failure_attempts
        self._last_reconcile_ms = None
        self._last_expiry_reconcile_ms = None

    def _review_pending_once(
        self,
        outcome,
        *,
        signal_id,
        snapshot,
        token,
        deadline,
        stop_requested,
        diagnostics,
        metrics,
    ):
        """One bounded same-lease wake + fresh admission, never a new task."""
        worker = self.worker
        wait = getattr(getattr(worker, "entry_filter", None), "wait_pending", None)
        if (
            outcome.get("status") != "DEFERRED"
            or outcome.get("reason") != "JEV_RESULT_PENDING"
            or not callable(wait)
        ):
            return outcome

        def remaining():
            if drain_requested(stop_requested):
                raise _DrainAttempt()
            budget = (
                2000 if deadline is None else int((deadline - self.monotonic()) * 1000)
            )
            if budget <= 0:
                return 0
            with self._connect() as conn:
                conn.execute("SET LOCAL lock_timeout='50ms'")
                conn.execute("SET LOCAL statement_timeout='100ms'")
                row = conn.execute(
                    """SELECT greatest(0,extract(epoch FROM (lease_until-clock_timestamp()))*1000)::bigint
                    FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s
                    AND lease_token=%s AND completed_at IS NULL""",
                    (worker.scope.consumer, signal_id, token),
                ).fetchone()
            return max(0, min(2000, budget, row[0] if row is not None else 0))

        review = {"signal_id": signal_id, "rechecked": False, "woke": False}
        diagnostics.setdefault("jev_same_cycle", []).append(review)
        budget = remaining()
        if budget <= 0:
            review["reason"] = "SCHEDULER_OR_LEASE_BUDGET"
            return outcome
        diagnostics["stage"] = "JEV_WAIT"
        started = time.perf_counter()
        woke = wait(
            signal_id=signal_id,
            consumer=worker.scope.consumer,
            max_wait_ms=budget,
            stop_requested=lambda: (
                drain_requested(stop_requested)
                or (deadline is not None and self.monotonic() >= deadline)
            ),
        )
        elapsed = (time.perf_counter() - started) * 1000
        metrics["jev_wait_ms"] = metrics.get("jev_wait_ms", 0) + elapsed
        review["wait_ms"], review["woke"] = round(elapsed), woke is True
        if drain_requested(stop_requested):
            raise _DrainAttempt()
        if woke is not True or remaining() <= 0:
            review["reason"] = "NO_TIMELY_RESULT_OR_LEASE"
            return outcome
        # No old balance/context is reused. Normal admission transport quotas
        # still apply; a local quota miss is DEFERRED, never management credit.
        diagnostics["stage"] = "CONTEXT"
        context_started = time.perf_counter()
        fresh = (
            self.context_provider(deepcopy(snapshot))
            if worker.runtime._now() < snapshot["expires_at_ms"]
            else {}
        )
        metrics["jev_recheck_context_ms"] = (
            metrics.get("jev_recheck_context_ms", 0)
            + (time.perf_counter() - context_started) * 1000
        )
        if remaining() <= 0:
            review["reason"] = "BUDGET_EXPIRED_DURING_FRESH_CONTEXT"
            return outcome
        diagnostics["stage"] = "CONSUME"
        review["rechecked"] = True
        metrics["jev_recheck_count"] = metrics.get("jev_recheck_count", 0) + 1
        return worker.consume(signal_id, context=fresh)

    def expire_pending(
        self, *, limit=1000, reconcile_seconds=None, hot_only=False, budget_ms=None
    ):
        """Retire never-decided expired signals in one bounded transaction.

        The original inbox and immutable receipt remain the audit. Signal locks
        serialize with StrategyWorker's first-decision commit. Recheck after
        acquiring locks so a concurrent committed decision is never discarded.
        Existing decisions/intents and active leases keep their recovery path.
        """
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("bounded expiry batch required")
        if budget_ms is not None and (
            type(budget_ms) is not int or not 1 <= budget_ms <= 5000
        ):
            raise ValueError("EXPIRY_QUERY_BUDGET_INVALID")
        if type(hot_only) is not bool or (hot_only and reconcile_seconds is None):
            raise ValueError("hot expiry requires an explicit reconciliation window")
        if reconcile_seconds is not None and (
            type(reconcile_seconds) is not int or not 1 <= reconcile_seconds <= 3600
        ):
            raise ValueError("bounded expiry reconciliation cadence required")
        started = time.perf_counter()
        worker, consumer, now = (
            self.worker,
            self.worker.scope.consumer,
            self.worker.runtime._now(),
        )
        # An intent belongs to one account/strategy consumer. Another account
        # or the opposite directional consumer must not pin this consumer's
        # expired input forever merely because the shared signal_id matches.
        intent_scope = tuple(asdict(worker.scope).values())
        reconcile = (
            reconcile_seconds is None
            or self._last_expiry_reconcile_ms is None
            or not 0 <= now - self._last_expiry_reconcile_ms < reconcile_seconds * 1000
        )
        reconcile = reconcile and not hot_only
        recent = "" if reconcile else "AND (s.snapshot->>'expires_at_ms')::bigint>%s"
        receipt_probe = "" if reconcile else "OFFSET 0"

        def complete(count, candidates):
            if reconcile:
                # A full batch may mean an old backlog; drain again next cycle
                # rather than parking it behind the reconciliation interval.
                self._last_expiry_reconcile_ms = now if candidates < limit else None
            self.last_expiry_metrics = {
                "full_reconciliation": reconcile,
                "reconciled_at_ms": self._last_expiry_reconcile_ms,
                "candidates": candidates,
                "expired": count,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
            return count

        with self._connect() as conn:
            if budget_ms is not None:
                conn.execute(
                    "SELECT set_config('statement_timeout',%s,true), "
                    "set_config('lock_timeout',%s,true)",
                    (f"{budget_ms}ms", f"{min(budget_ms, 250)}ms"),
                )
            conn.execute("SET LOCAL jit=off")
            # IDs first: avoid re-hashing historical wide snapshots after the
            # anti-join. Hot sweeps use the expiry index; full sweeps preserve
            # late commits and older lease releases without a high-water cursor.
            rows = conn.execute(
                f"""WITH pending AS MATERIALIZED (
                SELECT s.signal_id FROM v2_inbound_signals s
                WHERE s.source=%s AND s.environment=%s
                AND (s.snapshot->>'expires_at_ms')::bigint<=%s
                {recent}
                AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                    WHERE r.consumer=%s AND r.signal_id=s.signal_id {receipt_probe}))
                SELECT s.signal_id FROM pending p
                CROSS JOIN LATERAL (
                    SELECT i.signal_id,i.received_at FROM v2_inbound_signals i
                    WHERE i.signal_id=p.signal_id OFFSET 0) s
                WHERE NOT EXISTS (SELECT 1 FROM v2_strategy_decisions d
                    WHERE d.consumer=%s AND d.signal_id=s.signal_id OFFSET 0)
                AND NOT EXISTS (SELECT 1 FROM v2_trade_intents i
                    WHERE i.signal_id=s.signal_id
                    AND (i.exchange,i.account_id,i.environment,i.product,i.producer)=(%s,%s,%s,%s,%s) OFFSET 0)
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id
                    AND t.lease_until>=statement_timestamp() OFFSET 0)
                ORDER BY s.received_at,s.signal_id LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
                (
                    worker.source,
                    worker.scope.environment,
                    now,
                    *((now - reconcile_seconds * 1000,) if not reconcile else ()),
                    consumer,
                    consumer,
                    *intent_scope,
                    consumer,
                    limit,
                ),
            ).fetchall()
            if not rows:
                return complete(0, 0)
            # The broad discovery query uses a stable statement-time boundary:
            # clock_timestamp() is volatile and forces a correlated task lookup
            # for every historical signal, even ones already receipted. A lease
            # expiring during discovery is conservatively left for next time.
            # Recheck live leases below after locking each candidate signal, so
            # a lease renewed/acquired during discovery is never overwritten.
            expired = conn.execute(
                """INSERT INTO v2_signal_receipts(consumer,signal_id,outcome,reason)
                SELECT %s,s.signal_id,'EXPIRED','signal expired before scheduling'
                FROM v2_inbound_signals s WHERE s.signal_id=ANY(%s::uuid[])
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_decisions d
                    WHERE d.consumer=%s AND d.signal_id=s.signal_id)
                AND NOT EXISTS (SELECT 1 FROM v2_trade_intents i
                    WHERE i.signal_id=s.signal_id
                    AND (i.exchange,i.account_id,i.environment,i.product,i.producer)=(%s,%s,%s,%s,%s))
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id
                    AND t.lease_until>=clock_timestamp())
                ON CONFLICT DO NOTHING RETURNING signal_id""",
                (consumer, [row[0] for row in rows], consumer, *intent_scope, consumer),
            ).fetchall()
            if expired:
                conn.execute(
                    """INSERT INTO v2_strategy_tasks(consumer,signal_id,completed_at)
                    SELECT %s,unnest(%s::uuid[]),clock_timestamp()
                    ON CONFLICT(consumer,signal_id) DO UPDATE SET
                        completed_at=EXCLUDED.completed_at,error_code=NULL""",
                    (consumer, [row[0] for row in expired]),
                )
        return complete(len(expired), len(rows))

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

    def demand(self, *, budget_ms=1000, limit=1000):
        """Capped live actionable backlog; advisory only, never admission proof.

        No historical totals and no received-at cursor. Backoff, live leases and
        already receipted inputs cannot inflate a consumer's evaluation share.
        The count stops at the caller's useful weight cap. Query/lock waits have
        their own short limits; the pipeline may fall back when they expire.
        Recovery still gets a nonzero budget even without live signal demand.
        """
        if type(budget_ms) is not int or not 1 <= budget_ms <= 5000:
            raise ValueError("DEMAND_QUERY_BUDGET_INVALID")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("DEMAND_QUERY_LIMIT_INVALID")
        worker, consumer = self.worker, self.worker.scope.consumer
        with self._connect() as conn:
            conn.execute(
                "SELECT set_config('statement_timeout',%s,true), "
                "set_config('lock_timeout',%s,true)",
                (f"{budget_ms}ms", f"{min(budget_ms, 250)}ms"),
            )
            conn.execute("SET LOCAL jit=off")
            return conn.execute(
                """SELECT count(*) FROM (
                SELECT 1 FROM v2_inbound_signals s
                WHERE s.source=%s AND s.environment=%s
                AND (s.snapshot->>'expires_at_ms')::bigint>%s
                AND (s.snapshot->>'observed_at')::bigint<=%s
                AND s.snapshot->>'signal'=ANY(%s::text[])
                AND NOT EXISTS (SELECT 1 FROM v2_signal_receipts r
                    WHERE r.consumer=%s AND r.signal_id=s.signal_id OFFSET 0)
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id
                    AND (t.completed_at IS NOT NULL OR t.next_attempt_at>statement_timestamp()
                        OR t.lease_until>=statement_timestamp()) OFFSET 0)
                LIMIT %s) live_demand""",
                (
                    worker.source,
                    worker.scope.environment,
                    worker.runtime._now(),
                    worker.runtime._now(),
                    list(self.priority_events),
                    consumer,
                    consumer,
                    limit,
                ),
            ).fetchone()[0]

    def run_once(
        self,
        limit=100,
        *,
        interval_seconds=5,
        lease_seconds=300,
        budget_ms=None,
        stop_on_prepared=False,
        reconcile_seconds=60,
        stop_requested=None,
        hot_only=False,
        discover_only=False,
    ):
        if stop_requested is not None and not callable(stop_requested):
            raise TypeError("callable stop check required")
        if type(hot_only) is not bool or type(discover_only) is not bool:
            raise ValueError("explicit discovery mode required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid strategy batch limit")
        if any(
            type(v) is not int or not 1 <= v <= 3600
            for v in (interval_seconds, lease_seconds)
        ):
            raise ValueError("bounded strategy cadence and lease required")
        if budget_ms is not None and (
            type(budget_ms) is not int or not 1 <= budget_ms <= 60000
        ):
            raise ValueError("bounded strategy time budget required")
        if type(stop_on_prepared) is not bool:
            raise ValueError("explicit prepared-order yield required")
        if type(reconcile_seconds) is not int or not 1 <= reconcile_seconds <= 3600:
            raise ValueError("bounded discovery reconciliation cadence required")
        # Diagnostics use a separate clock, never alter the scheduling deadline.
        started = time.perf_counter()
        metrics = dict.fromkeys(
            ("discovery_ms", "claim_ms", "context_ms", "consume_ms", "ack_ms"), 0.0
        )
        self.last_run_metrics = metrics
        diagnostics = self.last_run_diagnostics = {"failures": [], "stage": "DISCOVERY"}
        if drain_requested(stop_requested):
            self.last_run_metrics = {
                **dict.fromkeys(metrics, 0),
                "processed": 0,
                "elapsed_ms": 0,
            }
            diagnostics.update(stage="COMPLETE", stop_reason="STOP_REQUESTED")
            return {}
        deadline = None if budget_ms is None else self.monotonic() + budget_ms / 1000
        worker, consumer = self.worker, self.worker.scope.consumer
        now = worker.runtime._now()
        # Hot discovery is indexed by expiry, never by a received-at cursor.
        # Thus late commits of still-valid signals are found every cycle. Full
        # anti-entropy remains necessary for old decisions and expired audits;
        # its cadence bounds that work without scanning history on every pass.
        reconcile = self._last_reconcile_ms is None or not (
            0 <= now - self._last_reconcile_ms < reconcile_seconds * 1000
        )
        reconcile = reconcile and not hot_only
        live_filter = (
            "" if reconcile else "AND (s.snapshot->>'expires_at_ms')::bigint>%s"
        )
        # Prevent a hot, tiny input set being expanded into a full historical
        # task hash join by cardinality estimates. Cold anti-entropy may hash.
        task_probe = "" if reconcile else "OFFSET 0"
        # No high-water mark: late commits and previously admitted-but-unprepared
        # intents remain discoverable. Previously scheduled tasks never starve
        # discovery of later signals, including when earlier tasks are failing.
        with self._connect() as conn:
            conn.execute("SET LOCAL jit=off")
            conn.execute(
                f"""WITH undiscovered AS MATERIALIZED (
                SELECT s.signal_id FROM v2_inbound_signals s
                WHERE s.source=%s AND s.environment=%s
                {live_filter}
                AND NOT EXISTS (SELECT 1 FROM v2_strategy_tasks t
                    WHERE t.consumer=%s AND t.signal_id=s.signal_id {task_probe}))
                INSERT INTO v2_strategy_tasks(consumer,signal_id)
                SELECT %s,s.signal_id FROM undiscovered p
                CROSS JOIN LATERAL (
                    SELECT i.signal_id,i.snapshot,i.received_at FROM v2_inbound_signals i
                    WHERE i.signal_id=p.signal_id OFFSET 0) s
                ORDER BY CASE
                    WHEN EXISTS (SELECT 1 FROM v2_strategy_decisions d
                        WHERE d.consumer=%s AND d.signal_id=s.signal_id OFFSET 0) THEN 0
                    WHEN (s.snapshot->>'observed_at')::bigint<=%s
                        AND (s.snapshot->>'expires_at_ms')::bigint>%s THEN 1
                    ELSE 2 END,
                    CASE WHEN s.snapshot->>'signal'=ANY(%s::text[]) THEN 0 ELSE 1 END,
                    (s.snapshot->>'expires_at_ms')::bigint,s.received_at,s.signal_id
                LIMIT %s ON CONFLICT DO NOTHING""",
                (
                    worker.source,
                    worker.scope.environment,
                    *((now,) if not reconcile else ()),
                    consumer,
                    consumer,
                    consumer,
                    now,
                    now,
                    list(self.priority_events),
                    limit,
                ),
            )
        if reconcile:
            self._last_reconcile_ms = now
        results = {}
        metrics["discovery_ms"] = (time.perf_counter() - started) * 1000
        if discover_only:
            # Dedicated housekeeping: never claim, evaluate or admit an intent.
            return results
        for _ in range(limit):
            if drain_requested(stop_requested):
                diagnostics["stop_reason"] = "STOP_REQUESTED"
                break
            if deadline is not None and self.monotonic() >= deadline:
                diagnostics["stop_reason"] = "BUDGET_EXHAUSTED"
                break
            diagnostics["stage"] = "CLAIM"
            token = str(uuid4())
            claimed_at = time.perf_counter()
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
                    AND (%s OR s.snapshot->>'signal'=ANY(%s::text[])
                        OR t.error_code IS NOT NULL
                        OR EXISTS (SELECT 1 FROM v2_strategy_decisions d
                            WHERE d.consumer=%s AND d.signal_id=t.signal_id))
                    ORDER BY CASE
                        WHEN EXISTS (SELECT 1 FROM v2_strategy_decisions d
                            WHERE d.consumer=t.consumer AND d.signal_id=t.signal_id) THEN 0
                        WHEN (s.snapshot->>'observed_at')::bigint<=%s
                            AND (s.snapshot->>'expires_at_ms')::bigint>%s THEN 1
                        ELSE 2 END,
                        CASE WHEN s.snapshot->>'signal'=ANY(%s::text[]) THEN 0 ELSE 1 END,
                        (s.snapshot->>'expires_at_ms')::bigint,t.next_attempt_at,t.signal_id LIMIT 1
                    FOR UPDATE OF t SKIP LOCKED)
                    UPDATE v2_strategy_tasks t SET attempts=attempts+1,lease_token=%s,
                    lease_until=clock_timestamp()+%s*interval '1 second'
                    FROM due WHERE t.consumer=%s AND t.signal_id=due.signal_id
                    RETURNING t.signal_id::text,t.attempts,t.error_code""",
                    (
                        consumer,
                        worker.source,
                        worker.scope.environment,
                        list(results),
                        not hot_only or self.actionable_events is None,
                        list(self.actionable_events or ()),
                        consumer,
                        worker.runtime._now(),
                        worker.runtime._now(),
                        list(self.priority_events),
                        token,
                        lease_seconds,
                        consumer,
                    ),
                ).fetchone()
            metrics["claim_ms"] += (time.perf_counter() - claimed_at) * 1000
            if row is None:
                diagnostics["stop_reason"] = "NO_DUE_TASK"
                break
            signal_id, attempt, previous_error = row
            error = None
            wait_reason = None
            snapshot = None
            context_at = time.perf_counter()
            consuming_at = None
            diagnostics["stage"] = "READ"
            try:
                if drain_requested(stop_requested):
                    raise _DrainAttempt()
                with self._connect() as conn:
                    snapshot, stored, receipt = worker._read(conn, signal_id)
                if drain_requested(stop_requested):
                    raise _DrainAttempt()
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
                    diagnostics["stage"] = "CONTEXT_POLICY"
                    needs_context = self.context_required(deepcopy(snapshot))
                    if type(needs_context) is not bool:
                        raise TypeError("context policy must return bool")
                diagnostics["stage"] = "CONTEXT"
                if drain_requested(stop_requested):
                    raise _DrainAttempt()
                context = (
                    self.context_provider(deepcopy(snapshot)) if needs_context else {}
                )
                if drain_requested(stop_requested):
                    raise _DrainAttempt()
                consuming_at = time.perf_counter()
                diagnostics["stage"] = "CONSUME"
                metrics["context_ms"] += (consuming_at - context_at) * 1000
                outcome = worker.consume(signal_id, context=context)
                outcome = self._review_pending_once(
                    outcome,
                    signal_id=signal_id,
                    snapshot=snapshot,
                    token=token,
                    deadline=deadline,
                    stop_requested=stop_requested,
                    diagnostics=diagnostics,
                    metrics=metrics,
                )
                results[signal_id] = outcome["status"]
            except _DrainAttempt:
                # Only our lease-token guarded ACK below releases this attempt.
                # No decision/receipt is invented and no other lease is cleared.
                results[signal_id] = "DRAINED"
                diagnostics["stop_reason"] = "STOP_REQUESTED"
            except Exception as exc:  # noqa: BLE001 - poison signals cannot stop other tasks
                error = diagnostic_code(exc)
                # Explicit capacity/position gates are expected waits, not a
                # dependency failure that blocks unrelated prepared orders.
                wait_reason = candidate_wait_reason(exc, stage=diagnostics["stage"])
                results[signal_id] = "DEFERRED" if wait_reason else "UNAVAILABLE"
                if error in CANDIDATE_CONTEXT_WAITS and (
                    type(exc) is not ValueError or diagnostics["stage"] != "CONTEXT"
                ):
                    # The task's persisted cause is evidence for a later
                    # bounded abstention. Matching text from another stage or
                    # exception class must never become that prior proof.
                    error = type(exc).__name__
                if (
                    wait_reason is not None
                    and error in TERMINAL_CONTEXT_FAILURES
                    and previous_error == error
                    and attempt >= self.context_failure_attempts
                ):
                    try:
                        resolution = worker.reject_candidate_context(
                            signal_id,
                            cause=error,
                            lease_token=token,
                            attempt=attempt,
                            minimum_attempts=self.context_failure_attempts,
                        )
                    except Exception as resolution_error:  # noqa: BLE001 - failed durable resolution is a real dependency failure
                        diagnostics["stage"] = "CONTEXT_RESOLUTION"
                        error = diagnostic_code(resolution_error)
                        wait_reason = None
                        results[signal_id] = "UNAVAILABLE"
                    else:
                        if resolution is not None:
                            results[signal_id] = resolution["status"]
                            wait_reason = None
                            metrics["candidate_resolutions"] = (
                                metrics.get("candidate_resolutions", 0) + 1
                            )
                            diagnostics.setdefault("candidate_resolutions", []).append(
                                {
                                    "signal_id": signal_id,
                                    "cause": error,
                                    "attempt": attempt,
                                    "outcome": resolution["status"],
                                    "decision_id": resolution.get("decision_id"),
                                    "execution_authorized": False,
                                }
                            )
                if (
                    error
                    in EXPECTED_ADMISSION_WAITS
                    | EXPECTED_CONTEXT_WAITS
                    | CANDIDATE_CONTEXT_WAITS.keys()
                    and not isinstance(exc, ValueError)
                ):
                    error = type(exc).__name__
                diagnostics["failures"].append(
                    {
                        "signal_id": signal_id,
                        "attempt": attempt,
                        "stage": diagnostics["stage"],
                        "error_code": error,
                        "outcome": results[signal_id],
                    }
                )
            finally:
                if consuming_at is None:
                    metrics["context_ms"] += (time.perf_counter() - context_at) * 1000
                else:
                    metrics["consume_ms"] += (time.perf_counter() - consuming_at) * 1000
            delay = (
                interval_seconds
                if error is None
                else max(interval_seconds, min(300, 2 ** min(attempt, 9)))
            )
            if results[signal_id] == "DRAINED":
                delay = 0  # next owner may evaluate afresh; no artificial backoff
            if wait_reason is not None:
                # Persisted together with the cycle journal; tasks retain the
                # exact error code/backoff too. No decision, admission or receipt
                # is manufactured. Expiry handling remains context-independent.
                diagnostics.setdefault("candidate_waits", []).append(
                    {
                        "signal_id": signal_id,
                        "symbol": snapshot.get("symbol")
                        if isinstance(snapshot, dict)
                        else None,
                        "scope": "CANDIDATE",
                        "reason": wait_reason,
                        "error_code": error,
                        "retry_seconds": delay,
                        "signal_expires_at_ms": (
                            snapshot.get("expires_at_ms")
                            if isinstance(snapshot, dict)
                            else None
                        ),
                        "execution_authorized": False,
                    }
                )
            ack_at = time.perf_counter()
            diagnostics["stage"] = "ACK"
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
            except Exception as exc:  # noqa: BLE001 - lease expiry retries lost scheduling acknowledgement
                results[signal_id] = "UNAVAILABLE"
                diagnostics["failures"].append(
                    {
                        "signal_id": signal_id,
                        "attempt": attempt,
                        "stage": "ACK",
                        "error_code": diagnostic_code(exc),
                        "outcome": "UNAVAILABLE",
                    }
                )
            finally:
                metrics["ack_ms"] += (time.perf_counter() - ack_at) * 1000
            if drain_requested(stop_requested):
                diagnostics["stop_reason"] = "STOP_REQUESTED"
                break
            if stop_on_prepared and results[signal_id] == "PREPARED":
                diagnostics["stop_reason"] = "PREPARED_YIELD"
                break
        diagnostics.setdefault("stop_reason", "BATCH_LIMIT")
        diagnostics["stage"] = "COMPLETE"
        metrics.update(
            processed=len(results), elapsed_ms=(time.perf_counter() - started) * 1000
        )
        # Business evidence deliberately forbids floats; persist integer ms.
        self.last_run_metrics = {key: round(value) for key, value in metrics.items()}
        return results
