"""Recover the original S6/S8 stop from immutable PG decision evidence.

Never derives an old position's stop from a new signal or current configuration.
Current venue coverage and market freshness remain the guarded installer's job.
"""

from v2_core.partial_open_protection import PartialOpenProtection
from v2_core.strategy_plan import LegacyDirectionalPlan


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
        return LegacyDirectionalPlan.read(
            scope=self.scope,
            config=config,
            snapshot=snapshot,
            producer=producer,
            strategy_version=version,
        ).protection_spec(order)

    def ensure(self, order_id):
        return self.recovery.ensure(order_id, self.plan(order_id))
