"""Recover the original S6/S8 stop from immutable PG decision evidence.

Never derives an old position's stop from a new signal or current configuration.
Current venue coverage and market freshness remain the guarded installer's job.
"""

from dataclasses import asdict

from v2_core.ledger import amount
from v2_core.partial_open_protection import PartialOpenProtection
from v2_core.protection import ProtectionSpec


class DirectionalStopRecovery:
    def __init__(
        self,
        connect,
        request,
        *,
        scope,
        reference,
        clock_ms,
        allow_writes=False,
        excluded_position_symbols=(),
    ):
        self.connect, self.scope = connect, scope
        self.recovery = PartialOpenProtection(
            connect,
            request,
            scope=scope,
            reference=reference,
            clock_ms=clock_ms,
            allow_writes=allow_writes,
            excluded_position_symbols=excluded_position_symbols,
        )

    def plan(self, order_id):
        order = self.recovery.cancel.snapshot(order_id)
        with self.connect() as conn:
            config, snapshot, producer, version = conn.execute(
                """SELECT e.config,e.snapshot,i.producer,i.strategy_version
                FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
                WHERE i.intent_id=%s""",
                (order["episode_id"],),
            ).fetchone()
        if (
            version != "directional-admission-v2-1"
            or config.get("mode") != "TESTNET_ADMISSION_ONLY"
            or config.get("account_scope") != asdict(self.scope)
            or config.get("strategy") not in {"S6", "S8"}
            or producer != config["strategy"].lower()
        ):
            raise ValueError("DIRECTIONAL_STOP_EVIDENCE_REQUIRED")
        evaluation = snapshot["features"]["evaluation"]
        market, sizing, result = (
            evaluation["market_plan"],
            evaluation["sizing"],
            snapshot["result"],
        )
        expected_side = "BUY" if config["strategy"] == "S6" else "SELL"
        if (
            evaluation.get("admission_authorized") is not True
            or evaluation.get("execution_market_gate") != "MARKET_GATES_PASSED"
            or market["strategy"] != config["strategy"]
            or market["side"] != ("LONG" if expected_side == "BUY" else "SHORT")
            or sizing["reason"] != "SIZED"
            or result["action"] != "OPEN"
            or result["side"] != expected_side
            or order["side"] != expected_side
            or snapshot["symbol"] != order["symbol"]
            or amount(sizing["quantity"], positive=True)
            != amount(order["intent"]["quantity"], positive=True)
            or amount(result["quantity"], positive=True)
            != amount(sizing["quantity"], positive=True)
        ):
            raise ValueError("DIRECTIONAL_STOP_EVIDENCE_MISMATCH")
        return ProtectionSpec(
            order["episode_id"],
            order["symbol"],
            "SELL" if expected_side == "BUY" else "BUY",
            "STOP_MARKET",
            sizing["stop_price"],
        )

    def ensure(self, order_id):
        return self.recovery.ensure(order_id, self.plan(order_id))
