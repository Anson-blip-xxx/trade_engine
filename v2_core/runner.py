"""Submission/recovery orchestration over explicit exchange and risk ports.

No transport is constructed here. A successful committed PREPARED->SUBMITTING
transition is the single submission permit. Recovery only queries that identity;
absence is never permission to resend a possibly accepted exchange request.
"""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from v2_core.errors import SubmissionNotSent
from v2_core.ledger import Ledger, lock_order_episode
from v2_core.orders import Orders
from v2_core.scoping import predicate, require_scope, validate_scope
from v2_core.transport import ExchangeTransportError


@dataclass(frozen=True)
class ExchangeObservation:
    client_order_id: str
    status: str
    exchange_order_id: str | None = None
    fills: tuple = ()
    evidence: dict | None = None


@dataclass(frozen=True)
class RiskVerdict:
    allowed: bool
    reason: str
    evidence: dict | None = None

    def __post_init__(self):
        if (
            type(self.allowed) is not bool
            or not isinstance(self.reason, str)
            or not self.reason.strip()
        ):
            raise ValueError("explicit risk verdict and reason required")


class ExecutionRunner:
    def __init__(
        self,
        connection_factory,
        *,
        submit,
        query,
        risk_check,
        risk_reference=None,
        scope=None,
    ):
        if not all(callable(port) for port in (submit, query, risk_check)):
            raise TypeError("explicit submit, query and risk ports required")
        self._connect = connection_factory
        self.scope = validate_scope(scope)
        self.submit, self.query, self.risk_check = submit, query, risk_check
        if risk_reference is not None and not callable(risk_reference):
            raise TypeError("explicit risk reference provider required")
        self.risk_reference = risk_reference
        self.orders = Orders(connection_factory)
        self.ledger = Ledger(connection_factory)

    def snapshot(self, order_id, *, connection=None):
        with self._connect() if connection is None else nullcontext(connection) as conn:
            row = conn.execute(
                """SELECT o.order_id::text,o.client_order_id,o.leg,
                o.quantity::text,o.status,o.version,i.exchange,i.account_id,
                i.environment,i.product,i.payload,o.exchange_order_id,
                o.order_type,o.limit_price::text,o.time_in_force,o.request_evidence FROM v2_orders o
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE o.order_id=%s""",
                (order_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown order")
        data = dict(
            zip(
                (
                    "order_id",
                    "client_order_id",
                    "leg",
                    "quantity",
                    "status",
                    "version",
                    "exchange",
                    "account_id",
                    "environment",
                    "product",
                    "intent",
                    "exchange_order_id",
                    "order_type",
                    "limit_price",
                    "time_in_force",
                    "request_evidence",
                ),
                row,
                strict=True,
            )
        )
        require_scope(self.scope, data)
        data["symbol"] = data["intent"]["symbol"]
        side = data["intent"]["side"]
        data["side"] = (
            side if data["leg"] == "OPEN" else ("SELL" if side == "BUY" else "BUY")
        )
        data["reduce_only"] = data["leg"] == "CLOSE"
        return data

    def dispatch(self, order_id):
        before = self.snapshot(order_id)
        if before["status"] != "PREPARED":
            return self.recover(order_id)
        verdict = self.risk_check(deepcopy(before))
        allowed = (
            verdict.allowed if isinstance(verdict, RiskVerdict) else verdict is True
        )
        if not allowed:
            cancelled = self.orders.transition(
                order_id,
                expected_version=before["version"],
                status="CANCELLED",
                evidence={"reason": verdict.reason, "risk": verdict.evidence or {}}
                if isinstance(verdict, RiskVerdict)
                else {"reason": "risk policy denied"},
            )
            return "DENIED" if cancelled else "RACE_LOST"
        # Reference I/O is outside DB locks; its age and the original decision
        # deadline are checked again under the account lock before committing.
        reference = (
            self.risk_reference(deepcopy(before))
            if before["leg"] == "OPEN" and self.risk_reference is not None
            else None
        )
        from v2_core.account_risk import AccountRiskDenied

        # If transaction commit raises, this function exits before exchange I/O.
        try:
            permitted = self.orders.transition(
                order_id,
                expected_version=before["version"],
                status="SUBMITTING",
                risk_reference=reference,
                require_account_risk=self.risk_reference is not None,
                evidence={
                    "reason": "durable dispatch",
                    "risk": {
                        "allowed": True,
                        "reason": verdict.reason
                        if isinstance(verdict, RiskVerdict)
                        else "injected policy allowed",
                        "evidence": (verdict.evidence or {})
                        if isinstance(verdict, RiskVerdict)
                        else {},
                    },
                },
            )
        except AccountRiskDenied as exc:
            cancelled = self.orders.transition(
                order_id,
                expected_version=before["version"],
                status="CANCELLED",
                evidence={
                    "reason": "account risk denied",
                    "account_risk_code": str(exc),
                    "account_risk_evidence": exc.evidence,
                },
            )
            return "DENIED" if cancelled else "RACE_LOST"
        if not permitted:
            return "RACE_LOST"
        try:
            observation = self.submit(deepcopy(before))
        except SubmissionNotSent as exc:
            applied = self.orders.transition(
                order_id,
                expected_version=before["version"] + 1,
                status="REJECTED",
                evidence={"reason": exc.reason, "submission_sent": False},
            )
            return "REJECTED" if applied else "RACE_LOST"
        except Exception as exc:  # noqa: BLE001 - transport may have accepted the order
            evidence = {"reason": "submission response unavailable"}
            if isinstance(exc, ExchangeTransportError):
                evidence["transport"] = exc.diagnostic_evidence()
            self.orders.transition(
                order_id,
                expected_version=before["version"] + 1,
                status="UNKNOWN",
                evidence=evidence,
            )
            return "UNKNOWN"
        return self._apply(order_id, observation)

    def recover(self, order_id):
        current = self.snapshot(order_id)
        if current["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
            return current["status"]
        if current["status"] == "PREPARED":
            return "PREPARED"
        # Query failure propagates; durable SUBMITTING/UNKNOWN remains recoverable.
        observation = self.query(current)
        if observation is None:
            return "UNKNOWN"
        return self._apply(order_id, observation)

    def _apply(self, order_id, observation):
        # Identity checks, fills and outcome must commit together. A malformed
        # second fill or conflicting outcome rolls the entire observation back.
        with self._connect() as conn:
            lock_order_episode(conn, order_id)
            return self._apply_locked(conn, order_id, observation)

    def _apply_locked(self, conn, order_id, observation):
        current = self.snapshot(order_id, connection=conn)
        ledger = Ledger(lambda: nullcontext(conn))
        orders = Orders(lambda: nullcontext(conn))
        if not isinstance(observation, ExchangeObservation):
            raise TypeError("typed exchange observation required")
        if observation.client_order_id != current["client_order_id"]:
            raise ValueError("observation belongs to a different order")
        if current[
            "exchange_order_id"
        ] is not None and observation.exchange_order_id not in (
            None,
            current["exchange_order_id"],
        ):
            raise ValueError("exchange order identity cannot change")
        if observation.status not in {
            "ACKNOWLEDGED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "UNKNOWN",
        }:
            raise ValueError("unsupported exchange observation")
        if (
            current["status"] in {"FILLED", "CANCELLED", "REJECTED"}
            and current["status"] != observation.status
        ):
            raise ValueError("conflicting terminal exchange observation")
        for fill in observation.fills:
            if not isinstance(fill, dict) or "order_id" in fill:
                raise ValueError("fill must be bound by the persisted order identity")
            ledger.record_fill(order_id=order_id, **fill)
        if current["status"] == observation.status and not (
            current["exchange_order_id"] is None
            and observation.exchange_order_id is not None
            and current["status"] in {"UNKNOWN", "ACKNOWLEDGED"}
        ):
            return observation.status
        if current["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
            raise ValueError("conflicting terminal exchange observation")
        applied = orders.transition(
            order_id,
            expected_version=current["version"],
            status=observation.status,
            evidence=observation.evidence or {},
            exchange_order_id=observation.exchange_order_id,
        )
        return observation.status if applied else "RACE_LOST"

    def recover_batch(self, limit=100, *, interval_seconds=5, lease_seconds=300):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid recovery batch limit")
        if any(
            type(v) is not int or not 1 <= v <= 3600
            for v in (interval_seconds, lease_seconds)
        ):
            raise ValueError("bounded recovery cadence and lease required")
        token = str(uuid4())
        condition, params = predicate(self.scope)
        with self._connect() as conn:
            conn.execute(
                f"""INSERT INTO v2_order_recovery(order_id)
                SELECT o.order_id FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE {condition} AND o.status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')
                AND NOT EXISTS (SELECT 1 FROM v2_order_recovery r WHERE r.order_id=o.order_id)
                ORDER BY o.updated_at,o.order_id LIMIT %s ON CONFLICT DO NOTHING""",
                (*params, limit),
            )
            claimed = conn.execute(
                f"""WITH due AS (
                SELECT r.order_id FROM v2_order_recovery r JOIN v2_orders o USING(order_id)
                JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE {condition} AND o.status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')
                AND r.next_attempt_at<=clock_timestamp()
                AND (r.lease_until IS NULL OR r.lease_until<clock_timestamp())
                ORDER BY r.next_attempt_at,r.order_id LIMIT %s FOR UPDATE OF r SKIP LOCKED
                ) UPDATE v2_order_recovery r SET attempts=attempts+1,lease_token=%s,
                lease_until=clock_timestamp()+%s*interval '1 second'
                FROM due WHERE r.order_id=due.order_id RETURNING r.order_id::text,r.attempts""",
                (*params, limit, token, lease_seconds),
            ).fetchall()
        results = {}
        for order_id, attempt in claimed:
            error = None
            try:
                results[order_id] = self.recover(order_id)
            except Exception as exc:  # noqa: BLE001 - isolate unavailable query/persistence per order
                results[order_id] = "UNAVAILABLE"
                error = type(exc).__name__
            delay = (
                interval_seconds
                if error is None
                else max(interval_seconds, min(300, 2 ** min(attempt, 9)))
            )
            try:
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE v2_order_recovery SET next_attempt_at=clock_timestamp()+%s*interval '1 second',
                        lease_token=NULL,lease_until=NULL,error_code=%s,attempts=%s
                        WHERE order_id=%s AND lease_token=%s""",
                        (
                            delay,
                            error,
                            0 if error is None else attempt,
                            order_id,
                            token,
                        ),
                    )
            except Exception:  # noqa: BLE001 - lease expiry recovers failed scheduling acknowledgement
                results[order_id] = "UNAVAILABLE"
        return results
