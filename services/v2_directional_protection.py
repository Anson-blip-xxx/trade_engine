"""Recover the original stop from immutable PG decision evidence.

Never derives an old position's stop from a new signal or current configuration.
Current venue coverage and market freshness remain the guarded installer's job.
"""

from dataclasses import asdict

from v2_core.ledger import amount
from v2_core.partial_open_protection import PartialOpenProtection
from v2_core.protection import ProtectionSpec
from v2_core.strategy_plan import LegacyDirectionalPlan
from v2_core.strategy_plan_store import read_trade_plan


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
            plan = read_trade_plan(conn, order["episode_id"])
            if plan is not None:
                # The reader binds the plan to committed intent/evidence. Bind
                # that same identity to the scoped opening snapshot as well;
                # a partial fill must not replace its original admitted size.
                if (
                    plan.scope != self.scope
                    or any(
                        order.get(key) != value
                        for key, value in asdict(self.scope).items()
                    )
                    or order["leg"] != "OPEN"
                    or order["symbol"] != plan.symbol
                    or order["side"] != plan.side
                    or order["intent"]["symbol"] != plan.symbol
                    or order["intent"]["side"] != plan.side
                    or amount(order["quantity"], positive=True)
                    != amount(plan.quantity, positive=True)
                    or amount(order["intent"]["quantity"], positive=True)
                    != amount(plan.quantity, positive=True)
                ):
                    raise ValueError("STRATEGY_STOP_EVIDENCE_MISMATCH")
                # Entry expiry, current deployment mode and current policy
                # cannot revoke protection for an already-created exposure.
                return ProtectionSpec(
                    order["episode_id"],
                    plan.symbol,
                    plan.exit_side,
                    "STOP_MARKET",
                    plan.stop_price,
                )
            config, snapshot, producer, version = conn.execute(
                """SELECT e.config,e.snapshot,i.producer,i.strategy_version
                FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
                WHERE i.intent_id=%s""",
                (order["episode_id"],),
            ).fetchone()
        return LegacyDirectionalPlan.read(
            scope=self.scope,
            config=config,
            snapshot=snapshot,
            producer=producer,
            strategy_version=version,
        ).protection_spec(order)

    def ensure(self, order_id):
        return self.recovery.ensure(order_id, self.plan(order_id))
