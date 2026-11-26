"""Audited termination of typed requests proven never submitted; no venue I/O.

Only the currently authorized MANAGE owner may act, including while DRAINING.
Revoked tokens cannot clean up their old work. The caller supplies the bound
same-transaction guard, not a cached authorization or caller-selected reason.
Route -> intent -> account -> episode/order is the existing lock order.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid5

from v2_core.account_risk import AccountScope
from v2_core.account_risk import _lock as lock_account
from v2_core.evidence import canonical
from v2_core.intents import IntentStore
from v2_core.ledger import amount, emit
from v2_core.orders import _NAMESPACE as ORDER_NAMESPACE
from v2_core.orders import Orders
from v2_core.scheduling import diagnostic_code
from v2_core.strategy_deployment import (
    _key,
    _read_current,
    _restore,
    _stored_payload,
    deployment_snapshot,
)
from v2_core.strategy_plan_admission import require_plan_admission
from v2_core.strategy_plan_store import read_trade_plan

_AUDIT = "STRATEGY_PENDING_CLEANUP"
_SCHEMA = "strategy-pending-cleanup-v1"
_CLOCK = "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"


def _stopped(check):
    if check is not None:
        if not callable(check):
            raise TypeError("STRATEGY_CLEANUP_STOP_CHECK_REQUIRED")
        value = check()
        if type(value) is not bool:
            raise TypeError("STRATEGY_CLEANUP_STOP_CHECK_REQUIRED")
        if value:
            raise ValueError("STRATEGY_CLEANUP_STOPPED")


def _binding(plan, snapshot, config):
    try:
        proof = snapshot["strategy_deployment"]
        frozen = _restore(
            {
                **{
                    key: proof[key]
                    for key in ("scope", "instance_id", "spec", "generation", "mode")
                },
                "config_json": canonical(config),
            }
        )
        if not (
            proof == deployment_snapshot(frozen)
            and frozen.mode == "ACTIVE"
            and frozen.scope == plan.scope
            and frozen.instance_id == plan.instance_id
            and frozen.generation == plan.deployment_generation
            and frozen.config_digest == plan.config_digest
            and frozen.spec.strategy_id == plan.strategy_id
            and frozen.spec.code_version == plan.code_version
            and frozen.spec.plan_schema == plan.schema_version
            and frozen.spec.exit_handler == plan.exit_handler
        ):
            raise ValueError("binding differs")
        return frozen
    except (KeyError, ValueError, TypeError, RecursionError):
        raise ValueError("STRATEGY_CLEANUP_ORIGINAL_PROOF_INVALID") from None


def _cause(plan, frozen, current, phase, now):
    if now >= plan.expires_at_ms:
        return "STRATEGY_PLAN_EXPIRED_BEFORE_SUBMISSION", "EXPIRED"
    if phase == "DRAINING":
        return "STRATEGY_ROUTE_DRAINING_BEFORE_SUBMISSION", "CANCELLED"
    if current.mode != "ACTIVE":
        return "STRATEGY_DEPLOYMENT_DISABLED_BEFORE_SUBMISSION", "CANCELLED"
    if current.binding_digest != frozen.binding_digest:
        return "STRATEGY_DEPLOYMENT_REPLACED_BEFORE_SUBMISSION", "CANCELLED"
    return None


def _audit(plan, frozen, current, phase, now, intent_id, order_id):
    if (
        type(now) is not int
        or now < plan.decided_at_ms
        or phase not in {"ACTIVE", "DRAINING"}
    ):
        raise ValueError("STRATEGY_CLEANUP_AUDIT_INVALID")
    cause = _cause(plan, frozen, current, phase, now)
    if cause is None:
        raise ValueError("STRATEGY_CLEANUP_AUDIT_INVALID")
    return {
        "schema": _SCHEMA,
        "intent_id": intent_id,
        "scope": asdict(plan.scope),
        "plan_digest": plan.plan_digest,
        "original_binding_digest": frozen.binding_digest,
        "current_deployment": asdict(current),
        "route_phase": phase,
        "observed_at_ms": now,
        "order_id": order_id,
        "reason": cause[0],
        "terminal_status": "CANCELLED" if order_id is not None else cause[1],
        "execution_authorized": False,
        "venue_flat_verified": False,
    }


class StrategyPendingCleanup:
    def __init__(self, connect, *, scope, commit_guard):
        if (
            not callable(connect)
            or type(scope) is not AccountScope
            or scope.environment != "SANDBOX"
            or not callable(commit_guard)
        ):
            raise TypeError("STRATEGY_CLEANUP_PORTS_REQUIRED")
        self.connect, self.commit_guard = connect, commit_guard
        self.scope = AccountScope(**asdict(scope))

    @staticmethod
    def _timeouts(conn):
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SET LOCAL statement_timeout='5s'")

    @staticmethod
    def _result(intent_id, status, **details):
        return {
            "intent_id": intent_id,
            "status": status,
            **details,
            "execution_authorized": False,
            "venue_flat_verified": False,
        }

    def cleanup(self, intent_id, *, stop_requested=None):
        intent_id = str(UUID(intent_id))
        _stopped(stop_requested)
        with self.connect() as conn:
            self._timeouts(conn)
            phase = self.commit_guard(conn, scope=self.scope, operation="MANAGE")
            if phase not in {"ACTIVE", "DRAINING"}:
                raise ValueError("STRATEGY_CLEANUP_TRANSACTION_FENCE_REQUIRED")
            _stopped(stop_requested)
            row = conn.execute(
                "SELECT i.status,e.snapshot,e.config FROM v2_trade_intents i "
                "JOIN v2_decision_evidence e USING(evidence_ref) WHERE i.intent_id=%s "
                "AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) "
                "FOR UPDATE OF i",
                (intent_id, *asdict(self.scope).values()),
            ).fetchone()
            if row is None:
                raise ValueError("STRATEGY_CLEANUP_INTENT_NOT_FOUND")
            lock_account(conn, self.scope.key)
            plan = read_trade_plan(conn, intent_id)
            if plan is None:
                raise ValueError("STRATEGY_CLEANUP_TYPED_PLAN_REQUIRED")
            frozen = _binding(plan, row[1], row[2])
            require_plan_admission(
                conn, intent_id, deployment=row[1]["strategy_deployment"]
            )
            result = self._cleanup_locked(conn, intent_id, row[0], plan, frozen, phase)
            _stopped(stop_requested)
        # The caller must recover the same identity after a lost commit ACK.
        return result

    def _cleanup_locked(self, conn, intent_id, status, plan, frozen, phase):
        orders = conn.execute(
            "SELECT order_id::text,status,version,exchange_order_id,leg,request_key,"
            "order_type,quantity,client_order_id,limit_price,time_in_force,request_evidence "
            "FROM v2_orders WHERE episode_id=%s ORDER BY order_id LIMIT 2",
            (intent_id,),
        ).fetchall()
        base = lambda state, **kw: self._result(intent_id, state, **kw)
        if status not in {"RECEIVED", "CANCELLED", "EXPIRED"} or len(orders) > 1:
            return base(
                "RECONCILIATION_REQUIRED", reason="STRATEGY_CLEANUP_NOT_PROVEN_UNSENT"
            )
        order = orders[0] if orders else None
        events = []
        if order is not None:
            expected_id = str(
                uuid5(ORDER_NAMESPACE, json.dumps([intent_id, "OPEN", "initial"]))
            )
            if (
                order[0] != expected_id
                or order[1] not in {"PREPARED", "CANCELLED"}
                or order[3] is not None
                or order[4:7] != ("OPEN", "initial", "MARKET")
                or order[7] != amount(plan.quantity, positive=True)
                or order[8] != "v2" + UUID(expected_id).hex
                or order[9:11] != (None, None)
                or conn.execute(
                    "SELECT 1 FROM v2_fills WHERE order_id=%s LIMIT 1", (order[0],)
                ).fetchone()
            ):
                return base(
                    "RECONCILIATION_REQUIRED",
                    reason="STRATEGY_CLEANUP_NOT_PROVEN_UNSENT",
                )
            events = conn.execute(
                "SELECT version,status,evidence FROM v2_order_events WHERE order_id=%s ORDER BY version LIMIT 3",
                (order[0],),
            ).fetchall()
            expected_states = (
                [(1, "PREPARED")]
                if order[1] == "PREPARED"
                else [(1, "PREPARED"), (2, "CANCELLED")]
            )
            if (
                [(event[0], event[1]) for event in events] != expected_states
                or order[2] != len(events)
                or events[0][2] != order[11]
            ):
                raise ValueError("STRATEGY_CLEANUP_ORDER_HISTORY_INVALID")
        episode = conn.execute(
            "SELECT status FROM v2_episodes WHERE episode_id=%s", (intent_id,)
        ).fetchone()
        if (
            episode != (("ABORTED",) if status == "CANCELLED" else ("ACTIVE",))
            and order is not None
        ):
            raise ValueError("STRATEGY_CLEANUP_EPISODE_STATE_INVALID")
        if order is None and episode is not None:
            raise ValueError("STRATEGY_CLEANUP_ORDER_FREE_EPISODE_INVALID")
        audits = conn.execute(
            "SELECT payload FROM v2_domain_outbox WHERE intent_id=%s AND event_type=%s LIMIT 2",
            (intent_id, _AUDIT),
        ).fetchall()
        if status in {"CANCELLED", "EXPIRED"}:
            if len(audits) != 1:
                raise ValueError("STRATEGY_CLEANUP_AUDIT_REQUIRED")
            try:
                audit = dict(audits[0][0])
                revision = audit.pop("data_revision")
                current = _restore(audit["current_deployment"])
                self._current_matches(plan, frozen, current)
                history = conn.execute(
                    "SELECT expected_version,payload,deleted,reason FROM v2_state_history "
                    "WHERE state_id=%s AND version=%s",
                    (
                        _key(current.scope, current.instance_id).identity,
                        current.generation,
                    ),
                ).fetchone()
                if (
                    type(revision) is not int
                    or revision < 1
                    or audit["route_phase"] not in {"ACTIVE", "DRAINING"}
                    or type(audit["observed_at_ms"]) is not int
                    or audit["observed_at_ms"] > conn.execute(_CLOCK).fetchone()[0]
                    or history
                    != (
                        current.generation - 1,
                        _stored_payload(current),
                        False,
                        "STRATEGY_DEPLOYMENT_PUBLISHED",
                    )
                    or audit
                    != _audit(
                        plan,
                        frozen,
                        current,
                        audit["route_phase"],
                        audit["observed_at_ms"],
                        intent_id,
                        None if order is None else order[0],
                    )
                    or audit["terminal_status"] != status
                    or (
                        order is not None
                        and (order[1] != "CANCELLED" or events[1][2] != audit)
                    )
                ):
                    raise ValueError("invalid audit")
            except (ValueError, TypeError, KeyError, RecursionError):
                raise ValueError("STRATEGY_CLEANUP_AUDIT_INVALID") from None
            if order is None and conn.execute(
                "SELECT payload->>'status',payload->>'reason' FROM v2_domain_outbox "
                "WHERE intent_id=%s AND event_type LIKE 'INTENT_TERMINAL:%%' LIMIT 2",
                (intent_id,),
            ).fetchall() != [(status, audit["reason"])]:
                raise ValueError("STRATEGY_CLEANUP_AUDIT_INVALID")
            return base(
                "ALREADY_TERMINATED", intent_status=status, reason=audit["reason"]
            )
        if audits or (order is not None and order[1] != "PREPARED"):
            raise ValueError("STRATEGY_CLEANUP_ROOT_STATE_CONFLICT")
        current = _read_current(conn, _key(self.scope, plan.instance_id))
        self._current_matches(plan, frozen, current)
        now = conn.execute(_CLOCK).fetchone()[0]
        cause = _cause(plan, frozen, current, phase, now)
        if cause is None:
            return base("UNCHANGED", reason="STRATEGY_ENTRY_STILL_CURRENT")
        audit = _audit(
            plan,
            frozen,
            current,
            phase,
            now,
            intent_id,
            None if order is None else order[0],
        )
        bound = lambda: nullcontext(conn)
        if order is None:
            changed = IntentStore(bound).terminate_unstarted(
                intent_id, status=audit["terminal_status"], reason=audit["reason"]
            )
        else:
            changed = Orders(bound).transition(
                order[0], expected_version=1, status="CANCELLED", evidence=audit
            )
        if not changed:
            raise ValueError("STRATEGY_CLEANUP_ROOT_STATE_CONFLICT")
        emit(conn, intent_id, _AUDIT, audit)
        return base(
            audit["terminal_status"], reason=audit["reason"], order_id=audit["order_id"]
        )

    @staticmethod
    def _current_matches(plan, frozen, current):
        if (
            current is None
            or current.scope != plan.scope
            or current.instance_id != plan.instance_id
            or current.generation < frozen.generation
            or (
                current.generation == frozen.generation
                and current.binding_digest != frozen.binding_digest
            )
        ):
            raise ValueError("STRATEGY_CLEANUP_CURRENT_DEPLOYMENT_INVALID")

    def run_once(self, *, limit=100, after=None, stop_requested=None):
        """One seek page, one transaction per intent; caller persists only a hint.

        Return to ``after=None`` after a short page. Failures also advance the
        hint; no page or status proves account flatness. Row limits are not wall
        time limits; independent process supervision remains the caller's job.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("STRATEGY_CLEANUP_LIMIT_INVALID")
        after = None if after is None else str(UUID(after))
        _stopped(stop_requested)
        with self.connect() as conn:
            self._timeouts(conn)
            rows = conn.execute(
                "SELECT i.intent_id::text FROM v2_trade_intents i "
                "JOIN v2_decision_evidence e USING(evidence_ref) "
                "WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s) "
                "AND e.snapshot ? 'strategy_plan' AND (i.status IN ('RECEIVED','UNKNOWN','EXECUTING') "
                "OR EXISTS (SELECT 1 FROM v2_orders o WHERE o.episode_id=i.intent_id AND o.status='PREPARED')) "
                "AND (%s::uuid IS NULL OR i.intent_id>%s::uuid) ORDER BY i.intent_id LIMIT %s",
                (*asdict(self.scope).values(), after, after, limit),
            ).fetchall()
        results = []
        for (intent_id,) in rows:
            _stopped(stop_requested)
            try:
                result = self.cleanup(intent_id, stop_requested=stop_requested)
            except Exception as exc:  # noqa: BLE001 - isolate only this candidate
                result = self._result(intent_id, "BLOCKED", reason=diagnostic_code(exc))
            results.append(result)
        return {
            "results": results,
            "next_after": rows[-1][0] if len(rows) == limit else None,
            "execution_authorized": False,
            "venue_flat_verified": False,
        }
