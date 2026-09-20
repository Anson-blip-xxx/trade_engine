"""Durable strategy evaluation -> intent admission -> PREPARED, never submission.

The first committed decision wins per scoped consumer/signal. Evaluation may run
more than once under contention, so the injected evaluator MUST be side-effect
free. Once stored, retries reuse its configuration/context/deadline verbatim.
"""

import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID, uuid5

from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.intents import OpenIntent
from v2_core.ledger import amount
from v2_core.state import normalized

_NAMESPACE = UUID("e1871725-a730-45d8-9c79-7d4044c9e530")


@dataclass(frozen=True)
class StrategyScope:
    exchange: str
    account_id: str
    environment: str
    product: str
    producer: str

    def __post_init__(self):
        for field in (
            self.exchange,
            self.account_id,
            self.environment,
            self.product,
            self.producer,
        ):
            normalized(field)

    @property
    def consumer(self):
        return json.dumps(
            [
                self.exchange,
                self.account_id,
                self.environment,
                self.product,
                self.producer,
            ],
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class StrategyDecision:
    action: str
    rationale: str
    side: str | None = None
    quantity: str | None = None
    features_json: str = "{}"

    def __post_init__(self):
        if self.action not in {"OPEN", "IGNORED"}:
            raise ValueError("explicit OPEN or IGNORED decision required")
        normalized(self.rationale)
        if self.action == "OPEN":
            if self.side not in {"BUY", "SELL"}:
                raise ValueError("explicit opening side required")
            amount(self.quantity, positive=True)
        elif self.side is not None or self.quantity is not None:
            raise ValueError("ignored signal cannot carry an order")
        object.__setattr__(
            self, "features_json", canonical(json.loads(self.features_json))
        )


class StrategyWorker:
    def __init__(
        self, runtime, scope, *, source, strategy_version, config, decide, max_delay_ms
    ):
        if not isinstance(scope, StrategyScope):
            raise TypeError("typed strategy scope required")
        for value in (source, strategy_version):
            normalized(value)
        if not callable(decide):
            raise TypeError("explicit pure evaluator required")
        if type(max_delay_ms) is not int or not 1 <= max_delay_ms <= 86400000:
            raise ValueError("bounded decision lifetime required")
        self.runtime, self.scope, self.source = runtime, scope, source
        self.strategy_version, self.config_json, self.decide, self.max_delay_ms = (
            strategy_version,
            canonical(config),
            decide,
            max_delay_ms,
        )
        self._connect = runtime.data._connect

    def _read(self, conn, signal_id):
        signal = conn.execute(
            "SELECT source,environment,snapshot FROM v2_inbound_signals WHERE signal_id=%s",
            (signal_id,),
        ).fetchone()
        if signal is None or signal[:2] != (self.source, self.scope.environment):
            raise ValueError("signal outside bound source/environment")
        row = conn.execute(
            """SELECT d.decision_id::text,d.action,d.side,d.quantity::text,
            e.strategy_version,e.config,e.snapshot FROM v2_strategy_decisions d
            JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE d.consumer=%s AND d.signal_id=%s""",
            (self.scope.consumer, signal_id),
        ).fetchone()
        receipt = conn.execute(
            "SELECT outcome,intent_id::text,reason FROM v2_signal_receipts WHERE consumer=%s AND signal_id=%s",
            (self.scope.consumer, signal_id),
        ).fetchone()
        return signal[2], row, receipt

    def decision(self, signal_id):
        with self._connect() as conn:
            _, row, _ = self._read(conn, str(UUID(signal_id)))
        if row is None:
            return None
        return dict(
            zip(
                (
                    "decision_id",
                    "action",
                    "side",
                    "quantity",
                    "strategy_version",
                    "config",
                    "snapshot",
                ),
                row,
                strict=True,
            )
        )

    def consume(self, signal_id, *, context):
        signal_id = str(UUID(signal_id))
        with self._connect() as conn:
            signal, stored, receipt = self._read(conn, signal_id)
        if stored is None and receipt is None:
            now = self.runtime._now()
            if signal["observed_at"] > now:
                return {"status": "DEFERRED", "signal_id": signal_id}
            context_json = canonical(context)
            if len(context_json.encode()) > 1_000_000:
                raise ValueError("decision context exceeds size limit")
            expired = now >= signal["expires_at_ms"]
            result = (
                StrategyDecision("IGNORED", "signal expired before evaluation")
                if expired
                else self.decide(
                    deepcopy(signal),
                    json.loads(context_json),
                    json.loads(self.config_json),
                )
            )
            if not isinstance(result, StrategyDecision):
                raise TypeError("typed strategy decision required")
            snapshot = {
                "signal_id": signal_id,
                "observed_at": signal["observed_at"],
                "decided_at": now,
                "expires_at_ms": min(signal["expires_at_ms"], now + self.max_delay_ms),
                "source": self.source,
                "symbol": signal["symbol"],
                "rationale": result.rationale,
                "features": {
                    "signal": signal["features"],
                    "context": json.loads(context_json),
                    "evaluation": json.loads(result.features_json),
                },
                "result": {
                    "action": "EXPIRED" if expired else result.action,
                    "side": result.side,
                    "quantity": result.quantity,
                },
            }
            proof = DecisionEvidence(
                self.strategy_version, self.config_json, canonical(snapshot)
            )
            if len((proof.config_json + proof.snapshot_json).encode()) > 1_000_000:
                raise ValueError("decision evidence exceeds size limit")
            identity = str(uuid5(_NAMESPACE, self.scope.consumer + ":" + signal_id))
            with self._connect() as conn:
                conn.execute(
                    "SELECT 1 FROM v2_inbound_signals WHERE signal_id=%s FOR UPDATE",
                    (signal_id,),
                )
                _, stored, receipt = self._read(conn, signal_id)
                if stored is None and receipt is None:
                    EvidenceStore(lambda: nullcontext(conn)).put(proof)
                    conn.execute(
                        """INSERT INTO v2_strategy_decisions(decision_id,consumer,signal_id,evidence_ref,action,side,quantity)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            identity,
                            self.scope.consumer,
                            signal_id,
                            proof.evidence_ref,
                            snapshot["result"]["action"],
                            result.side,
                            result.quantity,
                        ),
                    )
                    _, stored, receipt = self._read(conn, signal_id)
        if receipt is not None and (stored is None or receipt[0] != "INTENT"):
            return {
                "status": receipt[0],
                "intent_id": receipt[1],
                "reason": receipt[2],
                "signal_id": signal_id,
                "decision_id": None if stored is None else stored[0],
            }
        identity, action, side, quantity, version, config, snapshot = stored
        if action != "OPEN":
            self.runtime.data.signals.complete(
                consumer=self.scope.consumer,
                signal_id=signal_id,
                outcome=action,
                reason=snapshot["rationale"],
            )
            return {
                "status": action,
                "signal_id": signal_id,
                "decision_id": identity,
                "reason": snapshot["rationale"],
            }
        proof = DecisionEvidence(version, canonical(config), canonical(snapshot))
        intent = OpenIntent(
            identity,
            self.scope.exchange,
            self.scope.account_id,
            self.scope.environment,
            self.scope.product,
            self.scope.producer,
            "signal:" + signal_id,
            snapshot["symbol"],
            side,
            quantity,
            version,
            proof.config_digest,
            proof.evidence_ref,
        )
        result = self.runtime.accept_open(intent, proof)
        return {**result, "signal_id": signal_id, "decision_id": identity}
