"""Submission/recovery orchestration over explicit exchange and risk ports.

No transport is constructed here. A successful committed PREPARED->SUBMITTING
transition is the single submission permit. Recovery only queries that identity;
absence is never permission to resend a possibly accepted exchange request.
"""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass

from v2_core.ledger import Ledger, lock_order_episode
from v2_core.orders import Orders


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
    def __init__(self, connection_factory, *, submit, query, risk_check):
        if not all(callable(port) for port in (submit, query, risk_check)):
            raise TypeError("explicit submit, query and risk ports required")
        self._connect = connection_factory
        self.submit, self.query, self.risk_check = submit, query, risk_check
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
        # If transaction commit raises, this function exits before exchange I/O.
        if not self.orders.transition(
            order_id,
            expected_version=before["version"],
            status="SUBMITTING",
            evidence={"reason": "durable dispatch"},
        ):
            return "RACE_LOST"
        try:
            observation = self.submit(deepcopy(before))
        except Exception:  # noqa: BLE001 - transport may have accepted the order
            self.orders.transition(
                order_id,
                expected_version=before["version"] + 1,
                status="UNKNOWN",
                evidence={"reason": "submission response unavailable"},
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
        if current["status"] == observation.status:
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

    def recover_batch(self, limit=100):
        results = {}
        for order_id, _client, _status, _version in self.orders.recovery_candidates(
            limit
        ):
            try:
                results[order_id] = self.recover(order_id)
            except Exception:  # noqa: BLE001 - isolate unavailable query/persistence per order
                results[order_id] = "UNAVAILABLE"
        return results
