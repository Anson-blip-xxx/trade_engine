"""PG-owned order identity and audited state transitions. No exchange calls."""

import json
from uuid import UUID, uuid4, uuid5

from v2_core.evidence import canonical
from v2_core.ledger import amount, emit, lock_order_episode

_NAMESPACE = UUID("9697e015-22b4-4f6c-bda1-f7e81b6dd4aa")
_TRANSITIONS = {
    "PREPARED": {"SUBMITTING", "CANCELLED"},
    "SUBMITTING": {"UNKNOWN", "ACKNOWLEDGED", "FILLED", "REJECTED", "CANCELLED"},
    "UNKNOWN": {"ACKNOWLEDGED", "FILLED", "CANCELLED", "REJECTED"},
    "ACKNOWLEDGED": {"UNKNOWN", "FILLED", "CANCELLED"},
    "FILLED": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
}


class Orders:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def prepare(self, intent_id, *, leg="OPEN", quantity=None, request_key="initial"):
        intent_id = str(UUID(intent_id))
        if leg not in {"OPEN", "CLOSE"}:
            raise ValueError("invalid order leg")
        if (
            not isinstance(request_key, str)
            or not request_key
            or request_key != request_key.strip()
        ):
            raise ValueError("normalized order request key required")
        if leg == "OPEN" and request_key != "initial":
            raise ValueError("scale-in is not enabled")
        order_id = str(uuid5(_NAMESPACE, json.dumps([intent_id, leg, request_key])))
        client_id = "v2" + UUID(order_id).hex
        with self._connect() as conn:
            row = conn.execute(
                """SELECT exchange,account_id,environment,product,
                payload,status FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE""",
                (intent_id,),
            ).fetchone()
            if row is None or row[5] in {"REJECTED", "EXPIRED", "CANCELLED"}:
                raise ValueError("intent is missing or terminal")
            payload = row[4]
            requested = amount(
                payload["quantity"] if quantity is None else quantity, positive=True
            )
            if leg == "OPEN" and requested != amount(
                payload["quantity"], positive=True
            ):
                raise ValueError("opening quantity must match admitted intent")
            slot = json.dumps([*row[:4], payload["symbol"]], separators=(",", ":"))
            if leg == "OPEN":
                conn.execute(
                    """INSERT INTO v2_episodes(episode_id,slot_key)
                    VALUES (%s,%s) ON CONFLICT(episode_id) DO NOTHING""",
                    (intent_id, slot),
                )
            episode = conn.execute(
                "SELECT status FROM v2_episodes WHERE episode_id=%s FOR UPDATE",
                (intent_id,),
            ).fetchone()
            if episode != ("ACTIVE",):
                raise ValueError("episode is not active")
            existing = conn.execute(
                """SELECT quantity FROM v2_orders WHERE order_id=%s""", (order_id,)
            ).fetchone()
            if existing:
                if existing[0] != requested:
                    raise ValueError("order request content conflict")
                return order_id, client_id
            if leg == "CLOSE":
                opening = conn.execute(
                    """SELECT status FROM v2_orders
                    WHERE episode_id=%s AND leg='OPEN' """,
                    (intent_id,),
                ).fetchone()
                if opening is None or opening[0] not in {"FILLED", "CANCELLED"}:
                    raise ValueError(
                        "opening order must be terminal before close preparation"
                    )
                opened = conn.execute(
                    """SELECT COALESCE(sum(f.quantity),0)
                    FROM v2_fills f JOIN v2_orders o USING(order_id)
                    WHERE o.episode_id=%s AND o.leg='OPEN' """,
                    (intent_id,),
                ).fetchone()[0]
                closed = conn.execute(
                    """SELECT COALESCE(sum(f.quantity),0)
                    FROM v2_fills f JOIN v2_orders o USING(order_id)
                    WHERE o.episode_id=%s AND o.leg='CLOSE' """,
                    (intent_id,),
                ).fetchone()[0]
                reserved = conn.execute(
                    """SELECT COALESCE(sum(quantity-filled),0) FROM (
                    SELECT o.quantity,COALESCE(sum(f.quantity),0) AS filled
                    FROM v2_orders o LEFT JOIN v2_fills f USING(order_id)
                    WHERE o.episode_id=%s AND o.leg='CLOSE'
                      AND o.status NOT IN ('FILLED','CANCELLED','REJECTED')
                    GROUP BY o.order_id) reservations""",
                    (intent_id,),
                ).fetchone()[0]
                from decimal import localcontext

                with localcontext() as ctx:
                    ctx.prec = 80
                    available = opened - closed - reserved
                if requested > available:
                    raise ValueError("close quantity exceeds confirmed fills")
            conn.execute(
                """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,quantity,request_key)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (order_id, intent_id, leg, client_id, requested, request_key),
            )
            conn.execute(
                """INSERT INTO v2_order_events(event_id,order_id,version,status,evidence)
                VALUES (%s,%s,1,'PREPARED','{}')""",
                (str(uuid4()), order_id),
            )
            emit(
                conn,
                intent_id,
                "ORDER_PREPARED:" + order_id,
                {"order_id": order_id, "client_order_id": client_id, "leg": leg},
            )
        return order_id, client_id

    def transition(
        self, order_id, *, expected_version, status, evidence, exchange_order_id=None
    ):
        order_id = str(UUID(order_id))
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid expected version")
        encoded = canonical(evidence)
        with self._connect() as conn:
            lock_order_episode(conn, order_id)
            row = conn.execute(
                """SELECT status,version,episode_id::text,exchange_order_id,leg
                FROM v2_orders WHERE order_id=%s FOR UPDATE""",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown order")
            if row[1] != expected_version:
                return False
            if status not in _TRANSITIONS[row[0]]:
                raise ValueError("illegal order transition")
            if row[3] is not None and exchange_order_id not in (None, row[3]):
                raise ValueError("exchange order identity cannot change")
            if (
                status in {"ACKNOWLEDGED", "FILLED", "REJECTED", "CANCELLED"}
                and not evidence
            ):
                raise ValueError("exchange outcome requires evidence")
            if (
                status == "CANCELLED"
                and row[0] != "PREPARED"
                and evidence.get("fills_complete") is not True
            ):
                raise ValueError(
                    "cancelled exchange order requires complete fill reconciliation"
                )
            if status == "FILLED":
                quantities = conn.execute(
                    """SELECT o.quantity,COALESCE(sum(f.quantity),0)
                    FROM v2_orders o LEFT JOIN v2_fills f USING(order_id)
                    WHERE o.order_id=%s GROUP BY o.quantity""",
                    (order_id,),
                ).fetchone()
                if quantities[0] != quantities[1]:
                    raise ValueError("FILLED requires complete durable fill evidence")
            conn.execute(
                """UPDATE v2_orders SET status=%s,version=version+1,
                exchange_order_id=COALESCE(exchange_order_id,%s),updated_at=clock_timestamp()
                WHERE order_id=%s""",
                (status, exchange_order_id, order_id),
            )
            conn.execute(
                """INSERT INTO v2_order_events(event_id,order_id,version,status,evidence)
                VALUES (%s,%s,%s,%s,%s::jsonb)""",
                (str(uuid4()), order_id, expected_version + 1, status, encoded),
            )
            if row[4] == "OPEN":
                intent_status = {
                    "SUBMITTING": "EXECUTING",
                    "UNKNOWN": "UNKNOWN",
                    "ACKNOWLEDGED": "EXECUTING",
                    "FILLED": "FILLED",
                    "CANCELLED": "CANCELLED",
                    "REJECTED": "REJECTED",
                }[status]
                if status in {"CANCELLED", "REJECTED"}:
                    filled = conn.execute(
                        "SELECT 1 FROM v2_fills WHERE order_id=%s LIMIT 1", (order_id,)
                    ).fetchone()
                    if filled:
                        if status == "REJECTED":
                            raise ValueError(
                                "cannot reject an order with confirmed fills"
                            )
                        intent_status = "PARTIALLY_FILLED"
                    else:
                        conn.execute(
                            "UPDATE v2_episodes SET status='ABORTED' WHERE episode_id=%s",
                            (row[2],),
                        )
                conn.execute(
                    "UPDATE v2_trade_intents SET status=%s,version=version+1 WHERE intent_id=%s",
                    (intent_status, row[2]),
                )
            emit(
                conn,
                row[2],
                f"ORDER_STATE:{order_id}:{expected_version + 1}",
                {
                    "order_id": order_id,
                    "status": status,
                    "version": expected_version + 1,
                },
            )
        return True

    def recovery_candidates(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        with self._connect() as conn:
            return conn.execute(
                """SELECT order_id::text,client_order_id,status,version
                FROM v2_orders WHERE status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED')
                ORDER BY updated_at,order_id LIMIT %s""",
                (limit,),
            ).fetchall()
