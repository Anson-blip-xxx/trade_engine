"""Exact fill ledger for linear, single-episode positions.

No inferred exchange PnL and no Redis accumulators. Currency conversion is not
implicit: mixed fee currencies leave settlement pending for an explicit FX leg.
"""

import json
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID, uuid4

from v2_core.evidence import canonical


def amount(value, *, positive=False):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("amount requires a bounded decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if (
        not result.is_finite()
        or result.copy_abs() >= Decimal("1e20")
        or result.as_tuple().exponent < -18
        or (positive and result <= 0)
    ):
        raise ValueError("amount outside exact NUMERIC(38,18) domain")
    return result


def emit(conn, intent_id, kind, payload):
    # Every aggregate change increments one root version in the same transaction.
    # Cache rebuilds and event consumers can therefore fence stale snapshots.
    revision = conn.execute(
        """UPDATE v2_trade_intents SET data_revision=data_revision+1
        WHERE intent_id=%s RETURNING data_revision""",
        (intent_id,),
    ).fetchone()
    if revision is None:
        raise ValueError("unknown intent")
    conn.execute(
        """INSERT INTO v2_domain_outbox(event_id,intent_id,event_type,payload)
        VALUES (%s,%s,%s,%s::jsonb)""",
        (
            str(uuid4()),
            intent_id,
            kind,
            canonical(dict(payload, data_revision=revision[0])),
        ),
    )


def lock_order_episode(conn, order_id):
    """Acquire the aggregate root before any child lock (global lock order)."""
    row = conn.execute(
        """SELECT i.intent_id FROM v2_trade_intents i JOIN v2_orders o
        ON o.episode_id=i.intent_id WHERE o.order_id=%s FOR UPDATE OF i""",
        (order_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown order")


class Ledger:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    def record_fill(
        self,
        *,
        order_id,
        exchange_fill_id,
        quantity,
        price,
        fee,
        fee_currency,
        occurred_at_ms,
        evidence,
    ):
        order_id = str(UUID(order_id))
        qty, px, commission = (
            amount(quantity, positive=True),
            amount(price, positive=True),
            amount(fee),
        )
        if commission < 0 or not fee_currency or not exchange_fill_id:
            raise ValueError("invalid fee or fill identity")
        if type(occurred_at_ms) is not int or occurred_at_ms < 0:
            raise ValueError("invalid fill timestamp")
        encoded = canonical(evidence)
        with self._connect() as conn:
            lock_order_episode(conn, order_id)
            row = conn.execute(
                """SELECT o.episode_id::text, o.quantity, i.exchange,
                i.account_id,i.environment,i.product,i.payload->>'symbol',
                i.payload->>'side',o.leg,o.status
                FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
                WHERE o.order_id=%s FOR UPDATE OF o""",
                (order_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown order")
            conn.execute(
                "SELECT episode_id FROM v2_episodes WHERE episode_id=%s FOR UPDATE",
                (row[0],),
            )
            # Include the exchange-specific symbol scope: trade IDs need not be global.
            key = json.dumps([*row[2:7], exchange_fill_id], separators=(",", ":"))
            values = (
                order_id,
                qty,
                px,
                commission,
                fee_currency,
                occurred_at_ms,
                json.loads(encoded),
            )
            previous = conn.execute(
                """SELECT order_id::text,quantity,price,fee,
                fee_currency,occurred_at_ms,payload FROM v2_fills WHERE fill_key=%s""",
                (key,),
            ).fetchone()
            if previous is not None:
                if previous != values:
                    raise ValueError("fill identity content conflict")
                return False
            if row[9] not in {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED"}:
                raise ValueError("new fill requires a submitted nonterminal order")
            if conn.execute(
                "SELECT 1 FROM v2_settlements WHERE episode_id=%s", (row[0],)
            ).fetchone():
                raise ValueError(
                    "settled episode requires explicit accounting revision"
                )
            total = conn.execute(
                "SELECT COALESCE(sum(quantity),0) FROM v2_fills WHERE order_id=%s",
                (order_id,),
            ).fetchone()[0]
            with localcontext() as ctx:
                ctx.prec = 80
                if total + qty > row[1]:
                    raise ValueError(
                        "fill exceeds order quantity; reconciliation required"
                    )
            conn.execute(
                """INSERT INTO v2_fills
                (fill_key,order_id,quantity,price,fee,fee_currency,occurred_at_ms,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    key,
                    order_id,
                    qty,
                    px,
                    commission,
                    fee_currency,
                    occurred_at_ms,
                    encoded,
                ),
            )
            emit(
                conn,
                row[0],
                "FILL:" + key,
                {
                    "order_id": order_id,
                    "fill_key": key,
                    "exchange": row[2],
                    "account_id": row[3],
                    "environment": row[4],
                    "product": row[5],
                    "symbol": row[6],
                    "opening_side": row[7],
                    "leg": row[8],
                    "exchange_fill_id": exchange_fill_id,
                    "quantity": str(qty),
                    "price": str(px),
                    "fee": str(commission),
                    "fee_currency": fee_currency,
                    "occurred_at_ms": occurred_at_ms,
                    "evidence": json.loads(encoded),
                },
            )
            conn.execute(
                "UPDATE v2_episodes SET accounting_revision=accounting_revision+1 WHERE episode_id=%s",
                (row[0],),
            )
        return True

    def adjustment(
        self,
        *,
        episode_id,
        source_id,
        amount_text,
        currency,
        kind,
        occurred_at_ms,
        evidence,
    ):
        episode_id = str(UUID(episode_id))
        value = amount(amount_text)
        if kind not in {"FUNDING", "CORRECTION"} or not source_id or not currency:
            raise ValueError("invalid cash adjustment")
        if type(occurred_at_ms) is not int or occurred_at_ms < 0:
            raise ValueError("invalid timestamp")
        encoded = canonical(evidence)
        with self._connect() as conn:
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode_id,),
            )
            conn.execute(
                "SELECT episode_id FROM v2_episodes WHERE episode_id=%s FOR UPDATE",
                (episode_id,),
            )
            scope = conn.execute(
                """SELECT exchange,account_id,environment,product
                FROM v2_trade_intents WHERE intent_id=%s""",
                (episode_id,),
            ).fetchone()
            if scope is None:
                raise ValueError("unknown episode")
            key = json.dumps([*scope, kind, source_id], separators=(",", ":"))
            inserted = conn.execute(
                """INSERT INTO v2_cash_adjustments
                (adjustment_key,episode_id,amount,currency,kind,occurred_at_ms,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING
                RETURNING adjustment_key""",
                (key, episode_id, value, currency, kind, occurred_at_ms, encoded),
            ).fetchone()
            existing = conn.execute(
                """SELECT episode_id::text,amount,currency,kind,
                occurred_at_ms,evidence FROM v2_cash_adjustments WHERE adjustment_key=%s""",
                (key,),
            ).fetchone()
            if existing != (
                episode_id,
                value,
                currency,
                kind,
                occurred_at_ms,
                json.loads(encoded),
            ):
                raise ValueError("cash adjustment identity conflict")
            if inserted:
                conn.execute(
                    "UPDATE v2_episodes SET accounting_revision=accounting_revision+1 WHERE episode_id=%s",
                    (episode_id,),
                )
                emit(
                    conn,
                    episode_id,
                    "CASH:" + key,
                    {
                        "adjustment_key": key,
                        "source_id": source_id,
                        "amount": str(value),
                        "currency": currency,
                        "kind": kind,
                        "occurred_at_ms": occurred_at_ms,
                        "evidence": json.loads(encoded),
                    },
                )
        return bool(inserted)

    def report(self, episode_id, *, settlement_currency):
        episode_id = str(UUID(episode_id))
        with self._connect() as conn:
            # One consistent snapshot for an accounting report.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            intent = conn.execute(
                """SELECT i.payload,e.snapshot,e.config,i.exchange,i.product
                FROM v2_trade_intents i JOIN v2_decision_evidence e
                ON i.evidence_ref=e.evidence_ref WHERE intent_id=%s""",
                (episode_id,),
            ).fetchone()
            if intent is None:
                raise ValueError("unknown intent")
            if (
                intent[3:5] != ("BINANCE", "FUTURES")
                or settlement_currency not in {"USDT", "USDC"}
                or not intent[0]["symbol"].endswith(settlement_currency)
            ):
                raise ValueError("linear quote-settled futures report only")
            settled = (
                conn.execute(
                    """SELECT 1 FROM v2_settlements s JOIN v2_episodes e USING(episode_id)
                    WHERE s.episode_id=%s AND s.revision=e.accounting_revision""",
                    (episode_id,),
                ).fetchone()
                is not None
            )
            revision = conn.execute(
                "SELECT accounting_revision FROM v2_episodes WHERE episode_id=%s",
                (episode_id,),
            ).fetchone()
            fills = conn.execute(
                """SELECT o.leg,f.quantity,f.price,f.fee,f.fee_currency
                FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s""",
                (episode_id,),
            ).fetchall()
            adjustments = conn.execute(
                """SELECT amount,currency FROM v2_cash_adjustments
                WHERE episode_id=%s""",
                (episode_id,),
            ).fetchall()
        with localcontext() as ctx:
            ctx.prec = 100
            opened = sum((q for leg, q, p, f, c in fills if leg == "OPEN"), Decimal(0))
            closed = sum((q for leg, q, p, f, c in fills if leg == "CLOSE"), Decimal(0))
            entry = sum(
                (q * p for leg, q, p, f, c in fills if leg == "OPEN"), Decimal(0)
            )
            exit_value = sum(
                (q * p for leg, q, p, f, c in fills if leg == "CLOSE"), Decimal(0)
            )
            complete = opened > 0 and opened == closed
            currencies = {c for leg, q, p, f, c in fills if f != 0} | {
                c for a, c in adjustments if a != 0
            }
            currency_ok = currencies <= {settlement_currency}
            gross = (
                (exit_value - entry) * (1 if intent[0]["side"] == "BUY" else -1)
                if complete
                else None
            )
            fees = (
                sum((f for leg, q, p, f, c in fills), Decimal(0))
                if currency_ok
                else None
            )
            cash = sum((a for a, c in adjustments), Decimal(0)) if currency_ok else None
            net = gross - fees + cash if gross is not None and currency_ok else None
        return {
            "intent_id": episode_id,
            "accounting_revision": None if revision is None else revision[0],
            "decision": intent[1],
            "config": intent[2],
            "opened_quantity": str(opened),
            "closed_quantity": str(closed),
            "settlement_currency": settlement_currency,
            "gross_pnl": None if gross is None else str(gross),
            "fees": None if fees is None else str(fees),
            "cash_adjustments": None if cash is None else str(cash),
            "net_pnl": None if net is None else str(net),
            "accounting_status": ("SETTLED" if settled else "CALCULATED")
            if net is not None
            else "PENDING",
            "explanation": "成交价差、费用及资金调整；不构成策略因果证明",
        }

    def settle(self, episode_id, *, currency, evidence):
        """Require explicit reconciliation evidence; never infer flat from silence.

        The caller is the future authenticated exchange reconciler. This is a
        persistence contract, not an exchange verification implementation.
        """
        episode_id = str(UUID(episode_id))
        encoded = canonical(evidence)
        required = (
            "exchange_flat",
            "orders_terminal",
            "fills_complete",
            "cash_complete",
        )
        if any(evidence.get(key) is not True for key in required):
            raise ValueError("complete reconciliation evidence required")
        if (
            type(evidence.get("observed_at_ms")) is not int
            or evidence["observed_at_ms"] < 0
            or not evidence.get("source")
        ):
            raise ValueError("timestamped evidence source required")
        with self._connect() as conn:
            # All aggregate writes lock intent first, then children.
            contract = conn.execute(
                """SELECT exchange,product,payload->>'symbol'
                FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE""",
                (episode_id,),
            ).fetchone()
            if (
                contract is None
                or contract[:2] != ("BINANCE", "FUTURES")
                or currency not in {"USDT", "USDC"}
                or not contract[2].endswith(currency)
            ):
                raise ValueError("linear quote-settled futures settlement only")
            orders = conn.execute(
                """SELECT order_id, status FROM v2_orders
                WHERE episode_id=%s ORDER BY order_id FOR UPDATE""",
                (episode_id,),
            ).fetchall()
            episode = conn.execute(
                "SELECT status,accounting_revision FROM v2_episodes WHERE episode_id=%s FOR UPDATE",
                (episode_id,),
            ).fetchone()
            if episode is None:
                raise ValueError("unknown episode")
            latest = conn.execute(
                """SELECT max(occurred_at_ms) FROM (
                SELECT f.occurred_at_ms FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s UNION ALL SELECT occurred_at_ms
                FROM v2_cash_adjustments WHERE episode_id=%s) facts""",
                (episode_id, episode_id),
            ).fetchone()[0]
            if latest is not None and evidence["observed_at_ms"] < latest:
                raise ValueError("reconciliation evidence predates ledger facts")
            if (
                type(evidence.get("ledger_revision")) is not int
                or evidence["ledger_revision"] != episode[1]
            ):
                raise ValueError(
                    "reconciliation evidence is bound to a stale ledger revision"
                )
            existing = conn.execute(
                "SELECT currency,evidence FROM v2_settlements WHERE episode_id=%s AND revision=%s",
                (episode_id, episode[1]),
            ).fetchone()
            if existing:
                if existing != (currency, json.loads(encoded)):
                    raise ValueError("settlement evidence conflict")
                return False
            if not orders or any(
                row[1] not in {"FILLED", "CANCELLED", "REJECTED"} for row in orders
            ):
                raise ValueError("orders are not terminal")
            totals = conn.execute(
                """SELECT o.leg,sum(f.quantity)
                FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s GROUP BY o.leg""",
                (episode_id,),
            ).fetchall()
            totals = dict(totals)
            if totals.get("OPEN", 0) <= 0 or totals.get("OPEN") != totals.get("CLOSE"):
                raise ValueError("fill quantities do not reconcile")
            foreign_currency = conn.execute(
                """SELECT 1 FROM v2_fills f JOIN v2_orders o USING(order_id)
                WHERE o.episode_id=%s AND f.fee<>0 AND f.fee_currency<>%s
                UNION ALL SELECT 1 FROM v2_cash_adjustments
                WHERE episode_id=%s AND amount<>0 AND currency<>%s LIMIT 1""",
                (episode_id, currency, episode_id, currency),
            ).fetchone()
            if foreign_currency:
                raise ValueError("explicit currency conversion required")
            conn.execute(
                """INSERT INTO v2_settlements(episode_id,revision,currency,evidence)
                VALUES (%s,%s,%s,%s::jsonb)""",
                (episode_id, episode[1], currency, encoded),
            )
            conn.execute(
                "UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s",
                (episode_id,),
            )
            emit(
                conn,
                episode_id,
                "SETTLED:" + str(episode[1]),
                {"currency": currency, "evidence": evidence},
            )
        return True
