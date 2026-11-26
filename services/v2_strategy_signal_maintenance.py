"""Bounded, audited metadata maintenance; never venue I/O or entry authority.

Unknown facts, no setup and no budget are not negative trading decisions. Raw
signals remain pending until their original deadline, or an observed EVENT_END
proves there is no entry event. Frozen candidates and original receipts always
win. All mutations use the current MANAGE route/account fence, even DRAINING.
The independent job owner, not this module, supplies the hard wall-time bound.
"""

import json
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from uuid import UUID, uuid5

from services.v2_execution_guard import create_execution_guard
from services.v2_management_runtime import ManagementBinding
from v2_core.account_risk import _lock as lock_account
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical, digest
from v2_core.scheduling import diagnostic_code
from v2_core.session_lock import session_advisory_lock
from v2_core.signals import Signals
from v2_core.strategy import StrategyScope
from v2_core.strategy_deployment import (
    _key,
    _read_current,
    _restore,
    _stored_payload,
    deployment_snapshot,
)
from v2_core.strategy_entry_store import (
    _CLOCK_SQL,
    StrategyEntryStore,
    _decision_matches,
    _proof_size,
)
from v2_core.strategy_entry_worker import EntryReceipt
from v2_core.strategy_pending_cleanup import StrategyPendingCleanup
from v2_core.strategy_registry import strategy_identifier
from v2_core.strategy_signal_window import (
    deployment_history_page,
    has_consumer_work,
    initial_signal_cutoff,
)

RAW_SCHEMA = "strategy-raw-signal-resolution-v1"
RAW_EXPIRED = "STRATEGY_RAW_SIGNAL_EXPIRED"
RAW_ENDED = "STRATEGY_RAW_SIGNAL_LIFECYCLE_ENDED"
_NAMESPACE = UUID("456ff089-aa6a-4ac9-adab-d9de106631de")
_SOURCES = frozenset({"s2", "s3", "tv_bridge", "quant"})


def _raw_outcome(signal, now):
    """Only original source facts; no evaluation status or local wall clock."""
    if (
        type(now) is not int
        or type(signal.get("observed_at")) is not int
        or type(signal.get("expires_at_ms")) is not int
        or not 0 <= signal["observed_at"] < signal["expires_at_ms"]
    ):
        raise ValueError("STRATEGY_RAW_SIGNAL_TIME_INVALID")
    if now < signal["observed_at"]:
        return None
    if now >= signal["expires_at_ms"]:
        return "EXPIRED", RAW_EXPIRED
    if signal["signal"] == "EVENT_END":
        return "IGNORED", RAW_ENDED
    return None


def _raw_proof(binding, signal, *, signal_id, source, decided_at, phase):
    cause = _raw_outcome(signal, decided_at)
    if cause is None or phase not in {"ACTIVE", "DRAINING"}:
        raise ValueError("STRATEGY_RAW_TERMINAL_NOT_PROVEN")
    return _proof_size(
        DecisionEvidence(
            binding.spec.code_version,
            binding.config_json,
            canonical(
                {
                    "signal_id": signal_id,
                    "observed_at": signal["observed_at"],
                    "decided_at": decided_at,
                    "expires_at_ms": signal["expires_at_ms"],
                    "source": source,
                    "symbol": signal["symbol"],
                    "rationale": cause[1],
                    "features": {
                        "strategy_raw_resolution": {
                            "schema": RAW_SCHEMA,
                            "consumer": binding.consumer,
                            "scope": asdict(binding.scope),
                            "source_signal_digest": digest(canonical(signal)),
                            "deployment": deployment_snapshot(binding),
                            "route_phase": phase,
                        }
                    },
                    "result": {"action": cause[0], "side": None, "quantity": None},
                    "execution_authorized": False,
                    "venue_flat_verified": False,
                }
            ),
        )
    )


def _historical_deployment(conn, binding):
    # A self-consistent hash is not proof of an operator-published deployment.
    row = conn.execute(
        "SELECT payload,deleted,expected_version,reason FROM v2_state_history "
        "WHERE state_id=%s AND version=%s",
        (_key(binding.scope, binding.instance_id).identity, binding.generation),
    ).fetchone()
    if row != (
        _stored_payload(binding),
        False,
        binding.generation - 1,
        "STRATEGY_DEPLOYMENT_PUBLISHED",
    ):
        raise ValueError("STRATEGY_RAW_DEPLOYMENT_HISTORY_INVALID")


class MaintainedEntryStore(StrategyEntryStore):
    """Original typed recovery plus one narrowly verified raw terminal schema."""

    def _terminal_receipt(
        self, conn, *, scope, source, signal_id, signal, now, receipt, decision, intents
    ):
        snapshot = {} if decision is None else decision[8]
        features = snapshot.get("features", {})
        if "strategy_raw_resolution" not in features:
            return super()._terminal_receipt(
                conn,
                scope=scope,
                source=source,
                signal_id=signal_id,
                signal=signal,
                now=now,
                receipt=receipt,
                decision=decision,
                intents=intents,
            )
        try:
            resolution = features["strategy_raw_resolution"]
            deployment = resolution["deployment"]
            binding = _restore(
                {
                    **{
                        name: deployment[name]
                        for name in (
                            "scope",
                            "instance_id",
                            "spec",
                            "generation",
                            "mode",
                        )
                    },
                    "config_json": canonical(decision[7]),
                }
            )
            decided = snapshot["decided_at"]
            if (
                intents
                or receipt[1] is not None
                or type(decided) is not int
                or decided > now
                or binding.consumer != scope.consumer
                or binding.scope.environment != "SANDBOX"
                or resolution["schema"] != RAW_SCHEMA
                or _raw_outcome(signal, decided) != (receipt[0], receipt[2])
                or self._candidate(
                    conn,
                    scope=scope,
                    source=source,
                    signal_id=signal_id,
                    signal=signal,
                    now=now,
                )
                is not None
            ):
                raise ValueError("conflicting original work")
            _historical_deployment(conn, binding)
            proof = _raw_proof(
                binding,
                signal,
                signal_id=signal_id,
                source=source,
                decided_at=decided,
                phase=resolution["route_phase"],
            )
            _decision_matches(
                decision,
                proof,
                identity=str(uuid5(_NAMESPACE, scope.consumer + ":" + signal_id)),
                action=receipt[0],
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("STRATEGY_RAW_TERMINAL_EVIDENCE_INVALID") from None
        return EntryReceipt(receipt[0], receipt[2], decision_id=decision[0])


class SignalMaintenance:
    """One source/strategy/account; metadata only, independent of management."""

    def __init__(
        self,
        connect,
        *,
        binding,
        producer,
        source,
        stop,
        guard=None,
        deployment_window=False,
    ):
        strategy_identifier(producer)
        if (
            not callable(connect)
            or type(binding) is not ManagementBinding
            or binding.scope.environment != "SANDBOX"
            or source not in _SOURCES
            or not callable(getattr(stop, "is_set", None))
            or type(deployment_window) is not bool
        ):
            raise ValueError("STRATEGY_MAINTENANCE_PORTS_REQUIRED")
        guard = (
            create_execution_guard(connect, **binding.guard_args)
            if guard is None
            else guard
        )
        if getattr(guard, "_args", None) != binding.guard_args or not callable(
            getattr(guard, "require_transaction", None)
        ):
            raise ValueError("STRATEGY_MAINTENANCE_GUARD_REQUIRED")
        self.connect, self.binding, self.guard = connect, binding, guard
        self.scope = StrategyScope(**asdict(binding.scope), producer=producer)
        self.source, self.stop = source, stop
        self.deployment_window = deployment_window
        self.store = MaintainedEntryStore(connect)
        self.cleanup = StrategyPendingCleanup(
            connect, scope=binding.scope, commit_guard=guard.require_transaction
        )

    def _stopped(self):
        value = self.stop.is_set()
        if type(value) is not bool or value:
            raise ValueError("STRATEGY_MAINTENANCE_STOPPED")

    @contextmanager
    def transaction(self):
        self._stopped()
        with self.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute("SET LOCAL statement_timeout='5s'")
            phase = self.guard.require_transaction(
                conn, scope=self.binding.scope, operation="MANAGE"
            )
            if phase not in {"ACTIVE", "DRAINING"}:
                raise ValueError("STRATEGY_MAINTENANCE_TRANSACTION_FENCE_REQUIRED")
            self._stopped()
            yield conn, phase
            self._stopped()

    @staticmethod
    def _result(status, **details):
        return {
            "status": status,
            **details,
            "execution_authorized": False,
            "venue_flat_verified": False,
            "trading_health": "NOT_EVALUATED",
        }

    def maintain(self, signal_id):
        signal_id = self.store._identity(self.scope, self.source, signal_id)
        with self.transaction() as (conn, phase):
            self.store._signal(
                conn,
                scope=self.scope,
                source=self.source,
                signal_id=signal_id,
                lock="UPDATE",
            )
            current = self.store.read(
                conn, scope=self.scope, source=self.source, signal_id=signal_id
            )
            if current.receipt is not None:
                return self._result(
                    "RECOVERY_REQUIRED",
                    outcome=current.receipt.outcome,
                    reason="STRATEGY_EXISTING_VERIFIED_RECEIPT",
                )
            if current.candidate is not None:
                return self._result(
                    "RECOVERY_REQUIRED", reason="STRATEGY_FROZEN_CANDIDATE_REQUIRED"
                )
            signal = json.loads(current.snapshot_json)
            if self.deployment_window:
                cutoff = initial_signal_cutoff(conn, self.scope)
                if signal["expires_at_ms"] <= cutoff and not has_consumer_work(
                    conn, self.scope, signal_id
                ):
                    return self._result(
                        "OUT_OF_WINDOW",
                        reason="STRATEGY_SIGNAL_PREDATES_INITIAL_DEPLOYMENT",
                        initial_cutoff_ms=cutoff,
                    )
            if _raw_outcome(signal, current.now_ms) is None:
                return self._result(
                    "DEFERRED", reason="STRATEGY_RAW_SIGNAL_STILL_CURRENT"
                )
            # Publication uses this same account lock; no current/expired state
            # or final clock is trusted from before acquiring it.
            lock_account(conn, self.binding.scope.key)
            deployment = _read_current(
                conn, _key(self.binding.scope, self.scope.producer)
            )
            if deployment is None:
                raise ValueError("STRATEGY_MAINTENANCE_DEPLOYMENT_REQUIRED")
            now = conn.execute(_CLOCK_SQL).fetchone()[0]
            cause = _raw_outcome(signal, now)
            if cause is None:
                raise ValueError("STRATEGY_RAW_TERMINAL_NOT_PROVEN")
            proof = _raw_proof(
                deployment,
                signal,
                signal_id=signal_id,
                source=self.source,
                decided_at=now,
                phase=phase,
            )
            bound = lambda: nullcontext(conn)
            EvidenceStore(bound).put(proof)
            conn.execute(
                "INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,"
                "evidence_ref,action,side,quantity) VALUES(%s,%s,%s,%s,%s,NULL,NULL)",
                (
                    str(uuid5(_NAMESPACE, self.scope.consumer + ":" + signal_id)),
                    self.scope.consumer,
                    signal_id,
                    proof.evidence_ref,
                    cause[0],
                ),
            )
            Signals(bound).complete(
                consumer=self.scope.consumer,
                signal_id=signal_id,
                outcome=cause[0],
                reason=cause[1],
            )
            verified = self.store.read(
                conn, scope=self.scope, source=self.source, signal_id=signal_id
            ).receipt
            if verified is None or verified.outcome != cause[0]:
                raise ValueError("STRATEGY_RAW_TERMINAL_NOT_PROVEN")
            return self._result(
                cause[0], reason=cause[1], decision_id=verified.decision_id
            )

    def _cursor(self):
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='500ms'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            row = conn.execute(
                "SELECT request_cursor,intent_cursor::text,next_lane FROM v2_strategy_maintenance_cursors "
                "WHERE (consumer,source,environment)=(%s,%s,%s)",
                (self.scope.consumer, self.source, self.scope.environment),
            ).fetchone()
        return (None, None, "signals") if row is None else row

    def _checkpoint(self, *, request, intent, next_lane, before):
        if (request, intent, next_lane) == before:
            return
        with self.transaction() as (conn, _phase):
            conn.execute(
                "INSERT INTO v2_strategy_maintenance_cursors "
                "(consumer,source,environment,request_cursor,intent_cursor,next_lane) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(consumer,source,environment) "
                "DO UPDATE SET request_cursor=EXCLUDED.request_cursor,"
                "intent_cursor=EXCLUDED.intent_cursor,next_lane=EXCLUDED.next_lane,updated_at=clock_timestamp() "
                "WHERE (v2_strategy_maintenance_cursors.request_cursor,"
                "v2_strategy_maintenance_cursors.intent_cursor,v2_strategy_maintenance_cursors.next_lane) IS DISTINCT FROM "
                "(EXCLUDED.request_cursor,EXCLUDED.intent_cursor,EXCLUDED.next_lane)",
                (
                    self.scope.consumer,
                    self.source,
                    self.scope.environment,
                    request,
                    intent,
                    next_lane,
                ),
            )

    def _signal_page(self, after, limit):
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='500ms'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            if self.deployment_window:
                rows = deployment_history_page(
                    conn, scope=self.scope, source=self.source, cursor=after, size=limit
                )
                return conn.execute(
                    "SELECT p.signal_id::text,p.request_key,"
                    "(r.signal_id IS NOT NULL OR c.signal_id IS NOT NULL) "
                    "FROM unnest(%s::uuid[],%s::text[]) p(signal_id,request_key) "
                    "LEFT JOIN v2_signal_receipts r ON r.signal_id=p.signal_id AND r.consumer=%s "
                    "LEFT JOIN v2_strategy_entry_candidates c ON c.signal_id=p.signal_id AND c.consumer=%s "
                    "ORDER BY p.request_key",
                    (
                        [row[0] for row in rows],
                        [row[1] for row in rows],
                        self.scope.consumer,
                        self.scope.consumer,
                    ),
                ).fetchall()
            # Limit the raw key page first; an anti-join must not scan an
            # unbounded completed history to find the next pending candidate.
            return conn.execute(
                "WITH page AS MATERIALIZED (SELECT signal_id,request_key "
                "FROM v2_inbound_signals WHERE source=%s AND environment=%s "
                "AND (%s::text IS NULL OR request_key>%s) ORDER BY request_key LIMIT %s) "
                "SELECT p.signal_id::text,p.request_key,"
                "(r.signal_id IS NOT NULL OR c.signal_id IS NOT NULL) "
                "FROM page p LEFT JOIN v2_signal_receipts r "
                "ON r.signal_id=p.signal_id AND r.consumer=%s "
                "LEFT JOIN v2_strategy_entry_candidates c "
                "ON c.signal_id=p.signal_id AND c.consumer=%s ORDER BY p.request_key",
                (
                    self.source,
                    self.scope.environment,
                    after,
                    after,
                    limit,
                    self.scope.consumer,
                    self.scope.consumer,
                ),
            ).fetchall()

    def _intent_page(self, after, limit):
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='500ms'")
            conn.execute("SET LOCAL lock_timeout='250ms'")
            return conn.execute(
                "SELECT i.intent_id::text FROM v2_trade_intents i "
                "JOIN v2_decision_evidence e USING(evidence_ref) WHERE "
                "(i.exchange,i.account_id,i.environment,i.product,i.producer)=(%s,%s,%s,%s,%s) "
                "AND e.snapshot->>'source'=%s AND e.snapshot ? 'strategy_plan' "
                "AND (i.status IN ('RECEIVED','UNKNOWN','EXECUTING') OR EXISTS "
                "(SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id AND o.status='PREPARED')) "
                "AND (%s::uuid IS NULL OR i.intent_id>%s::uuid) ORDER BY i.intent_id LIMIT %s",
                (*asdict(self.scope).values(), self.source, after, after, limit),
            ).fetchall()


def run_signal_maintenance(
    connect,
    *,
    binding,
    producer,
    source,
    stop,
    guard=None,
    limit=10,
    budget_ms=5000,
    deployment_window=False,
):
    """Two bounded persistent sweeps. A cursor is never a receipt or flat proof.

    Advance each hint before attempting its item, so even an owner-enforced
    process kill cannot repeatedly strand later work behind one poison record.
    An interrupted/skipped item is revisited after wrap. No result is a claim of
    queue exhaustion, order submission, account flatness or trading health.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= 20
        or type(budget_ms) is not int
        or not 100 <= budget_ms <= 60000
    ):
        raise ValueError("STRATEGY_MAINTENANCE_BOUNDS_REQUIRED")
    port = SignalMaintenance(
        connect,
        binding=binding,
        producer=producer,
        source=source,
        stop=stop,
        guard=guard,
        deployment_window=deployment_window,
    )
    port._stopped()
    started = time.monotonic()
    results = {"signals": {}, "cleanup": {}}
    with session_advisory_lock(
        connect, "typed-maintenance:" + port.scope.consumer + ":" + source
    ) as acquired:
        if not acquired:
            return port._result("BUSY", **results)
        with port.transaction():
            pass
        request, intent, first_lane = port._cursor()
        before = request, intent, first_lane
        next_lane = "cleanup" if first_lane == "signals" else "signals"
        # Persist the next *first* lane before work as well: a hard owner kill
        # during one slow item must not repeatedly starve the other lane.
        port._checkpoint(
            request=request, intent=intent, next_lane=next_lane, before=before
        )
        before = request, intent, next_lane
        readers = {"signals": port._signal_page, "cleanup": port._intent_page}
        for lane in (first_lane, next_lane):
            port._stopped()
            if (time.monotonic() - started) * 1000 >= budget_ms:
                break
            lane_started = time.monotonic()
            rows = readers[lane](request if lane == "signals" else intent, limit)
            complete = True
            for row in rows:
                port._stopped()
                if (time.monotonic() - lane_started) * 1000 >= budget_ms / 2:
                    complete = False
                    break
                identity = row[0]
                if lane == "signals":
                    request = row[1]
                else:
                    intent = identity
                port._checkpoint(
                    request=request, intent=intent, next_lane=next_lane, before=before
                )
                before = request, intent, next_lane
                if lane == "signals" and row[2]:
                    continue  # Verified recovery remains the typed worker's job.
                try:
                    result = (
                        port.maintain(identity)
                        if lane == "signals"
                        else port.cleanup.cleanup(identity, stop_requested=stop.is_set)
                    )
                except Exception as exc:  # noqa: BLE001 - isolate only one item
                    result = port._result("BLOCKED", reason=diagnostic_code(exc))
                results[lane][identity] = result
            if complete and len(rows) < limit:
                if lane == "signals":
                    request = None
                else:
                    intent = None
                port._checkpoint(
                    request=request, intent=intent, next_lane=next_lane, before=before
                )
                before = request, intent, next_lane
        return port._result(
            "COMPLETED", **results, request_cursor=request, intent_cursor=intent
        )
