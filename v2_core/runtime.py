"""New runtime composition boundary. All I/O ports are explicit dependencies.

This module does not import legacy executors, discover credentials, start loops,
or construct a live transport. Acceptance and dispatch are distinct operations.
"""

import json

from v2_core.attention import RecoveryAttention
from v2_core.intents import AdmissionCode
from v2_core.runner import ExecutionRunner, RiskVerdict
from v2_core.service import TradingData


class DataRuntime:
    def __init__(
        self,
        connection_factory,
        *,
        submit,
        query,
        risk_check,
        clock_ms,
        projectors=(),
        risk_reference=None,
    ):
        if not callable(clock_ms) or not callable(risk_check):
            raise TypeError("explicit clock and risk policy required")
        self.data = TradingData(connection_factory)
        self.clock_ms, self.risk_check = clock_ms, risk_check
        self.projectors = tuple(projectors)
        self.attention = RecoveryAttention(connection_factory)
        from v2_core.account_risk import AccountRisk

        self.account_risk = AccountRisk(connection_factory)
        self.execution = ExecutionRunner(
            connection_factory,
            submit=submit,
            query=query,
            risk_check=self._risk,
            risk_reference=risk_reference,
        )

    def _now(self):
        value = self.clock_ms()
        if type(value) is not int or value < 0:
            raise ValueError("clock must return nonnegative epoch milliseconds")
        return value

    @staticmethod
    def _fresh(snapshot, now):
        expires = snapshot.get("expires_at_ms")
        return (
            type(expires) is int
            and snapshot["observed_at"] <= snapshot["decided_at"] <= now < expires
        )

    def accept_open(self, intent, evidence, *, prepare_initial=True):
        """Persist first, then prepare or record a terminal rejection/expiry.

        The decision deadline is part of immutable evidence. Replaying the same
        request after expiry cannot replace that deadline or revive the intent.
        """
        if type(prepare_initial) is not bool:
            raise ValueError("explicit initial preparation flag required")
        snapshot = json.loads(evidence.snapshot_json)
        result = (
            self.data.accept_signal(intent, evidence)
            if "signal_id" in snapshot
            else self.data.accept(intent, evidence)
        )
        if result.code not in {AdmissionCode.ACCEPTED, AdmissionCode.ALREADY_ACCEPTED}:
            return {"status": result.code.value, "intent_id": result.intent_id}
        identity = result.intent_id
        trace = self.data.trace(identity)
        if trace["orders"] or trace["status"] != "RECEIVED":
            opening = next((o for o in trace["orders"] if o["leg"] == "OPEN"), None)
            return {
                "status": trace["status"] if opening is None else opening["status"],
                "intent_id": identity,
                "orders": trace["orders"],
                "order_id": None if opening is None else opening["order_id"],
                "client_order_id": None
                if opening is None
                else opening["client_order_id"],
            }
        snapshot = json.loads(evidence.snapshot_json)
        if not self._fresh(snapshot, self._now()):
            self.data.intents.terminate_unstarted(
                identity,
                status="EXPIRED",
                reason="decision expired or missing valid deadline",
            )
            return {
                "status": self.data.trace(identity)["status"],
                "intent_id": identity,
            }
        # Capability rejection is durable and explicit, not a silent fallback to
        # an executor whose accounting contract differs from this runtime.
        if (
            intent.exchange != "BINANCE"
            or intent.product != "FUTURES"
            or not intent.symbol.endswith(("USDT", "USDC"))
        ):
            self.data.intents.terminate_unstarted(
                identity, status="REJECTED", reason="unsupported accounting contract"
            )
            return {
                "status": self.data.trace(identity)["status"],
                "intent_id": identity,
            }
        if not prepare_initial:
            return {"status": "RECEIVED", "intent_id": identity}
        try:
            order_id, client_id = self.data.orders.prepare(identity)
        except Exception as exc:
            if (
                getattr(exc, "sqlstate", None) != "23505"
                or getattr(getattr(exc, "diag", None), "constraint_name", None)
                != "v2_one_active_episode"
            ):
                raise
            current = self.data.trace(identity)
            return {
                "status": "WAITING_CAPACITY"
                if current["status"] == "RECEIVED"
                else current["status"],
                "intent_id": identity,
                "reason": "symbol has an active episode",
            }
        return {
            "status": "PREPARED",
            "intent_id": identity,
            "order_id": order_id,
            "client_order_id": client_id,
        }

    def _risk(self, request):
        if request["leg"] == "OPEN":
            # Recheck at dispatch: an accepted decision may have waited in a queue.
            with self.data._connect() as conn:
                snapshot = conn.execute(
                    "SELECT snapshot FROM v2_decision_evidence WHERE evidence_ref=%s",
                    (request["intent"]["evidence_ref"],),
                ).fetchone()
            if snapshot is None or not self._fresh(snapshot[0], self._now()):
                return RiskVerdict(False, "decision expired before dispatch")
        return self.risk_check(request)

    def recover_once(self, limit=100):
        """Recovery never submits PREPARED or ambiguous orders."""
        return self.execution.recover_batch(limit)

    def expire_once(self, limit=100):
        """Terminate only requests proven not to have crossed submission CAS."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        now = self._now()
        with self.data._connect() as conn:
            rows = conn.execute(
                """SELECT i.intent_id::text,o.order_id::text,o.version
                FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
                LEFT JOIN v2_orders o ON o.episode_id=i.intent_id AND o.leg='OPEN'
                WHERE ((i.status='RECEIVED' AND o.order_id IS NULL) OR o.status='PREPARED')
                  AND CASE WHEN jsonb_typeof(e.snapshot->'expires_at_ms')='number'
                      THEN (e.snapshot->>'expires_at_ms')::numeric <= %s ELSE TRUE END
                ORDER BY i.created_at,i.intent_id LIMIT %s""",
                (now, limit),
            ).fetchall()
        results = {}
        for intent_id, order_id, version in rows:
            if order_id is None:
                changed = self.data.intents.terminate_unstarted(
                    intent_id,
                    status="EXPIRED",
                    reason="decision expired while awaiting preparation",
                )
            else:
                changed = self.data.orders.transition(
                    order_id,
                    expected_version=version,
                    status="CANCELLED",
                    evidence={
                        "reason": "decision expired before submission",
                        "observed_at_ms": now,
                    },
                )
            results[intent_id] = "EXPIRED" if changed else "RACE_LOST"
        return results

    def project_once(self, limit=100):
        results = {}
        for projector in self.projectors:
            try:
                results[projector.consumer] = projector.run_scheduled_batch(limit)
            except Exception as exc:  # noqa: BLE001 - isolate projection backends
                results[projector.consumer] = {"error": type(exc).__name__}
        return results

    def tick(self, *, overdue_ms, limit=100):
        """One supervisor iteration: no new order submission, no automatic resend.

        Storage/transport failures are visible and isolated by phase. Deployment
        chooses cadence; every injected network port must impose finite timeouts.
        """
        if type(overdue_ms) is not int or overdue_ms <= 0:
            raise ValueError("explicit positive attention threshold required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        results = {}
        for name, action in (
            ("expiry", lambda: self.expire_once(limit)),
            ("recovery", lambda: self.recover_once(limit)),
            ("risk_releases", lambda: self.account_risk.sweep_releases(limit)),
            (
                "attention",
                lambda: self.attention.scan(
                    now_ms=self._now(), overdue_ms=overdue_ms, limit=limit
                ),
            ),
            ("projection", lambda: self.project_once(limit)),
        ):
            try:
                results[name] = action()
            except Exception as exc:  # noqa: BLE001 - observable per-phase failures
                results[name] = {"error": type(exc).__name__}
        return results
