"""Composition root for the new data core; no legacy storage imports."""

from v2_core.evidence import EvidenceStore
from v2_core.intents import IntentStore
from v2_core.ledger import Ledger
from v2_core.orders import Orders


class TradingData:
    def __init__(self, connection_factory):
        self._connect = connection_factory
        self.evidence = EvidenceStore(connection_factory)
        self.intents = IntentStore(connection_factory)
        self.orders = Orders(connection_factory)
        self.ledger = Ledger(connection_factory)

    def accept(self, intent, evidence):
        if (
            intent.evidence_ref != evidence.evidence_ref
            or intent.config_digest != evidence.config_digest
            or intent.strategy_version != evidence.strategy_version
        ):
            raise ValueError("intent must reference the exact decision evidence")
        # Immutable evidence may precede admission; failed admission leaves no
        # actionable request. An orphan snapshot is harmless and auditable.
        self.evidence.put(evidence)
        return self.intents.admit(intent)

    def trace(self, intent_id):
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            intent = conn.execute(
                """SELECT i.intent_id::text,i.status,i.version,
                i.payload,e.config,e.snapshot,i.data_revision FROM v2_trade_intents i
                JOIN v2_decision_evidence e USING(evidence_ref)
                WHERE i.intent_id=%s""",
                (intent_id,),
            ).fetchone()
            if intent is None:
                return None
            episode = conn.execute(
                "SELECT status,accounting_revision FROM v2_episodes WHERE episode_id=%s",
                (intent_id,),
            ).fetchone()
            settlements = conn.execute(
                """SELECT revision,currency,evidence FROM v2_settlements
                WHERE episode_id=%s ORDER BY revision""",
                (intent_id,),
            ).fetchall()
            orders = conn.execute(
                """SELECT order_id::text,client_order_id,leg,status,version
                FROM v2_orders WHERE episode_id=%s ORDER BY order_id""",
                (intent_id,),
            ).fetchall()
            events = conn.execute(
                """SELECT e.event_id::text,e.order_id::text,e.version,
                e.status,e.evidence FROM v2_order_events e JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s ORDER BY e.created_at,e.event_id""",
                (intent_id,),
            ).fetchall()
            domain = conn.execute(
                """SELECT event_id::text,event_type,payload
                FROM v2_domain_outbox WHERE intent_id=%s ORDER BY created_at,event_id""",
                (intent_id,),
            ).fetchall()
            fills = conn.execute(
                """SELECT f.fill_key,f.order_id::text,f.quantity::text,
                f.price::text,f.fee::text,f.fee_currency,f.occurred_at_ms,f.payload
                FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s ORDER BY f.occurred_at_ms,f.fill_key""",
                (intent_id,),
            ).fetchall()
            cash = conn.execute(
                """SELECT adjustment_key,amount::text,currency,kind,
                occurred_at_ms,evidence FROM v2_cash_adjustments WHERE episode_id=%s
                ORDER BY occurred_at_ms,adjustment_key""",
                (intent_id,),
            ).fetchall()
        return {
            "intent_id": intent[0],
            "status": intent[1],
            "version": intent[2],
            "data_revision": intent[6],
            "episode": None
            if episode is None
            else dict(zip(("status", "accounting_revision"), episode, strict=True)),
            "settlements": [
                dict(zip(("revision", "currency", "evidence"), row, strict=True))
                for row in settlements
            ],
            "request": intent[3],
            "configuration": intent[4],
            "decision": intent[5],
            "domain_events": [
                dict(zip(("event_id", "kind", "payload"), row, strict=True))
                for row in domain
            ],
            "fills": [
                dict(
                    zip(
                        (
                            "fill_key",
                            "order_id",
                            "quantity",
                            "price",
                            "fee",
                            "fee_currency",
                            "occurred_at_ms",
                            "evidence",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in fills
            ],
            "cash": [
                dict(
                    zip(
                        (
                            "adjustment_key",
                            "amount",
                            "currency",
                            "kind",
                            "occurred_at_ms",
                            "evidence",
                        ),
                        row,
                        strict=True,
                    )
                )
                for row in cash
            ],
            "orders": [
                dict(
                    zip(
                        ("order_id", "client_order_id", "leg", "status", "version"),
                        row,
                        strict=True,
                    )
                )
                for row in orders
            ],
            "timeline": [
                dict(
                    zip(
                        ("event_id", "order_id", "version", "status", "evidence"),
                        row,
                        strict=True,
                    )
                )
                for row in events
            ],
        }
