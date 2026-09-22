"""PG-owned order identity and audited state transitions. No exchange calls."""

import json
from decimal import Decimal, localcontext
from uuid import UUID, uuid4, uuid5

from v2_core.evidence import canonical
from v2_core.ledger import amount, emit, lock_order_episode

_NAMESPACE = UUID("9697e015-22b4-4f6c-bda1-f7e81b6dd4aa")
_TRANSITIONS = {
    "PREPARED": {"SUBMITTING", "CANCELLED"},
    "SUBMITTING": {"UNKNOWN", "ACKNOWLEDGED", "FILLED", "REJECTED", "CANCELLED"},
    "UNKNOWN": {"ACKNOWLEDGED", "FILLED", "CANCELLED", "REJECTED"},
    "ACKNOWLEDGED": {"UNKNOWN", "FILLED", "CANCELLED", "REJECTED"},
    "FILLED": set(),
    "CANCELLED": set(),
    "REJECTED": set(),
}


class Orders:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def prepare(
        self,
        intent_id,
        *,
        leg="OPEN",
        quantity=None,
        request_key="initial",
        order_type="MARKET",
        limit_price=None,
        time_in_force=None,
        evidence=None,
    ):
        intent_id = str(UUID(intent_id))
        if leg not in {"OPEN", "CLOSE"}:
            raise ValueError("invalid order leg")
        if (
            not isinstance(request_key, str)
            or not request_key
            or request_key != request_key.strip()
        ):
            raise ValueError("normalized order request key required")
        encoded = canonical({} if evidence is None else evidence)
        if (
            leg == "OPEN"
            and request_key != "initial"
            and not json.loads(encoded).get("reason")
        ):
            raise ValueError("additional opening order requires decision reason")
        if order_type == "MARKET":
            if limit_price is not None or time_in_force is not None:
                raise ValueError(
                    "market order cannot carry a limit price or time-in-force"
                )
            price = None
        elif order_type == "LIMIT" and time_in_force in {"GTC", "IOC", "FOK"}:
            price = amount(limit_price, positive=True)
        else:
            raise ValueError("unsupported order type or time-in-force")
        order_id = str(uuid5(_NAMESPACE, json.dumps([intent_id, leg, request_key])))
        client_id = "v2" + UUID(order_id).hex
        with self._connect() as conn:
            row = conn.execute(
                """SELECT exchange,account_id,environment,product,
                payload,status FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE""",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise ValueError("intent is missing or terminal")
            payload = row[4]
            requested = amount(
                payload["quantity"] if quantity is None else quantity, positive=True
            )
            existing = conn.execute(
                """SELECT quantity,order_type,limit_price,time_in_force,request_evidence
                FROM v2_orders WHERE order_id=%s""",
                (order_id,),
            ).fetchone()
            if existing:
                if existing != (
                    requested,
                    order_type,
                    price,
                    time_in_force,
                    json.loads(encoded),
                ):
                    raise ValueError("order request content conflict")
                return order_id, client_id
            if row[5] in {"REJECTED", "EXPIRED", "CANCELLED"}:
                raise ValueError("intent is missing or terminal")
            if leg == "OPEN":
                from v2_core.opening_halt import require_opening_allowed

                require_opening_allowed(conn, intent_id)
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
            if leg == "OPEN":
                allocated = conn.execute(
                    """SELECT COALESCE(sum(CASE WHEN status IN ('CANCELLED','REJECTED')
                        THEN filled ELSE quantity END),0) FROM (
                        SELECT o.order_id,o.status,o.quantity,COALESCE(sum(f.quantity),0) AS filled
                        FROM v2_orders o LEFT JOIN v2_fills f USING(order_id)
                        WHERE o.episode_id=%s AND o.leg='OPEN' GROUP BY o.order_id
                    ) allocations""",
                    (intent_id,),
                ).fetchone()[0]
                with localcontext() as ctx:
                    ctx.prec = 80
                    if allocated + requested > amount(
                        payload["quantity"], positive=True
                    ):
                        raise ValueError("opening allocation exceeds admitted quantity")
            else:
                request_evidence = json.loads(encoded)
                native_child = request_evidence.get("origin") == "BINANCE_ALGO_CHILD"
                if native_child and not (
                    isinstance(request_evidence.get("parent_algo_id"), int)
                    and request_evidence["parent_algo_id"] > 0
                    and request_key
                    == "native-algo:" + str(request_evidence["parent_algo_id"])
                    and all(
                        isinstance(request_evidence.get(field), str)
                        and bool(request_evidence[field])
                        for field in (
                            "parent_client_algo_id",
                            "exchange_order_id",
                            "venue_client_order_id",
                        )
                    )
                    and request_evidence["exchange_order_id"].isascii()
                    and request_evidence["exchange_order_id"].isdecimal()
                    and int(request_evidence["exchange_order_id"]) > 0
                ):
                    raise ValueError("invalid exchange-created close binding")
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
                with localcontext() as ctx:
                    ctx.prec = 80
                    # A registered native stop may trigger while a cooperating
                    # reduce-only exit is in flight. It already exists at the
                    # exchange, so represent it even though local reservations
                    # overlap; Binance prevents either order reversing exposure.
                    available = (
                        opened - closed - (Decimal(0) if native_child else reserved)
                    )
                if requested > available:
                    raise ValueError("close quantity exceeds confirmed fills")
            conn.execute(
                """INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,quantity,request_key,
                order_type,limit_price,time_in_force,request_evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    order_id,
                    intent_id,
                    leg,
                    client_id,
                    requested,
                    request_key,
                    order_type,
                    price,
                    time_in_force,
                    encoded,
                ),
            )
            conn.execute(
                """INSERT INTO v2_order_events(event_id,order_id,version,status,evidence)
                VALUES (%s,%s,1,'PREPARED',%s::jsonb)""",
                (str(uuid4()), order_id, encoded),
            )
            emit(
                conn,
                intent_id,
                "ORDER_PREPARED:" + order_id,
                {
                    "order_id": order_id,
                    "client_order_id": client_id,
                    "leg": leg,
                    "quantity": str(requested),
                    "order_type": order_type,
                    "limit_price": None if price is None else str(price),
                    "time_in_force": time_in_force,
                    "evidence": json.loads(encoded),
                },
            )
        return order_id, client_id

    def transition(
        self,
        order_id,
        *,
        expected_version,
        status,
        evidence,
        exchange_order_id=None,
        risk_reference=None,
        require_account_risk=False,
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
            binds_identity = (
                status == row[0]
                and row[0] in {"UNKNOWN", "ACKNOWLEDGED"}
                and row[3] is None
                and exchange_order_id is not None
            )
            if status not in _TRANSITIONS[row[0]] and not binds_identity:
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
            if (
                status == "REJECTED"
                and conn.execute(
                    "SELECT 1 FROM v2_fills WHERE order_id=%s LIMIT 1", (order_id,)
                ).fetchone()
            ):
                raise ValueError("cannot reject an order with confirmed fills")
            if row[4] == "OPEN" and status == "SUBMITTING":
                from v2_core.account_risk import reserve_open
                from v2_core.opening_halt import require_opening_allowed

                require_opening_allowed(conn, row[2])

                proof = reserve_open(
                    conn,
                    order_id,
                    reference=risk_reference,
                    required=require_account_risk,
                )
                if proof is not None:
                    encoded = canonical({**json.loads(encoded), "account_risk": proof})
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
                states = {
                    s[0]
                    for s in conn.execute(
                        "SELECT status FROM v2_orders WHERE episode_id=%s AND leg='OPEN'",
                        (row[2],),
                    ).fetchall()
                }
                filled = conn.execute(
                    """SELECT COALESCE(sum(f.quantity),0) FROM v2_fills f
                    JOIN v2_orders o USING(order_id) WHERE o.episode_id=%s AND o.leg='OPEN'""",
                    (row[2],),
                ).fetchone()[0]
                budget = conn.execute(
                    "SELECT (payload->>'quantity')::numeric FROM v2_trade_intents WHERE intent_id=%s",
                    (row[2],),
                ).fetchone()[0]
                if "UNKNOWN" in states:
                    intent_status = "UNKNOWN"
                elif states & {"SUBMITTING", "ACKNOWLEDGED"}:
                    intent_status = "EXECUTING"
                elif "PREPARED" in states:
                    intent_status = "RECEIVED" if filled == 0 else "PARTIALLY_FILLED"
                elif filled > 0:
                    intent_status = "FILLED" if filled == budget else "PARTIALLY_FILLED"
                else:
                    intent_status = (
                        "REJECTED" if states == {"REJECTED"} else "CANCELLED"
                    )
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
            from v2_core.account_risk import release_terminal

            release_terminal(conn, row[2])
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
