"""Durable, read-only Telegram projections for confirmed trade lifecycle facts."""

from dataclasses import asdict
from decimal import Decimal, localcontext

from v2_core.account_risk import AccountScope
from v2_core.delivery import Projector
from v2_core.ledger import Ledger, amount
from v2_core.service import TradingData
from v2_core.telegram import TelegramOperationalSink


def _exact(value):
    return format(Decimal(value).normalize(), "f")


def _fees(fills):
    grouped = {}
    for fill in fills:
        currency = fill["fee_currency"]
        grouped[currency] = grouped.get(currency, Decimal(0)) + amount(fill["fee"])
    return (
        ", ".join(f"{_exact(value)} {key}" for key, value in sorted(grouped.items()))
        or "0 USDT"
    )


def _average(trace, leg):
    order_ids = {row["order_id"] for row in trace["orders"] if row["leg"] == leg}
    fills = [row for row in trace["fills"] if row["order_id"] in order_ids]
    quantity = sum(
        (amount(row["quantity"], positive=True) for row in fills), Decimal(0)
    )
    if quantity <= 0:
        raise ValueError("CONFIRMED_TRADE_FILLS_REQUIRED")
    with localcontext() as context:
        context.prec = 100
        value = sum(
            amount(row["quantity"], positive=True) * amount(row["price"], positive=True)
            for row in fills
        )
        price = value / quantity
    return fills, quantity, price, value


class TradeLifecycleProjector(Projector):
    def _scope_filter(self):
        scoped, params = super()._scope_filter()
        return (
            f"""({scoped}) AND EXISTS (
                SELECT 1 FROM v2_trade_intents ti WHERE ti.intent_id=e.intent_id
                AND ti.producer IN ('s6','s8')
                AND ti.strategy_version='directional-admission-v2-1') AND (
                (e.event_type LIKE 'ORDER_STATE:%%' AND e.payload->>'status'='FILLED'
                 AND EXISTS (SELECT 1 FROM v2_orders oo WHERE oo.episode_id=e.intent_id
                     AND oo.order_id=(e.payload->>'order_id')::uuid AND oo.leg='OPEN'))
                OR e.event_type LIKE 'SETTLED:%%')""",
            params,
        )


class TradeLifecycleNotifications:
    def __init__(self, connect, sink, *, scope):
        if (
            not isinstance(scope, AccountScope)
            or scope.environment != "SANDBOX"
            or not isinstance(sink, TelegramOperationalSink)
            or sink.environment != scope.environment
        ):
            raise ValueError("account-bound Testnet trade notifications required")
        self.connect, self.sink, self.scope = connect, sink, scope
        self.data = TradingData(connect)
        self.projector = TradeLifecycleProjector(
            connect,
            "trade-telegram-v1:" + scope.key,
            self._deliver,
            scope=scope,
        )

    def run_once(self, limit=10):
        return self.projector.run_scheduled_batch(limit, lease_seconds=30)

    def _facts(self, episode):
        trace = self.data.trace(episode)
        if (
            trace is None
            or any(
                trace["scope"].get(key) != value
                for key, value in asdict(self.scope).items()
            )
            or trace["scope"]["producer"] not in {"s6", "s8"}
            or trace["signal"] is None
        ):
            raise ValueError("DIRECTIONAL_NOTIFICATION_SCOPE_MISMATCH")
        evaluation = trace["decision"]["features"]["evaluation"]
        plan, sizing = evaluation["market_plan"], evaluation["sizing"]
        openings = [row for row in trace["orders"] if row["leg"] == "OPEN"]
        if len(openings) != 1:
            raise ValueError("SINGLE_OPENING_ORDER_REQUIRED")
        opening = openings[0]
        open_fills, quantity, entry, notional = _average(trace, "OPEN")
        with self.connect() as conn:
            order_identity = conn.execute(
                "SELECT exchange_order_id FROM v2_orders WHERE order_id=%s",
                (opening["order_id"],),
            ).fetchone()
            readiness = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope->>'namespace'='opening-readiness-v1' AND scope->>'key'=%s""",
                (opening["order_id"],),
            ).fetchone()
            protection = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope->>'namespace'='testnet-protection-v1'
                AND payload->'spec'->>'episode'=%s LIMIT 2""",
                (episode,),
            ).fetchall()
        if (
            order_identity is None
            or order_identity[0] is None
            or readiness is None
            or readiness[0].get("status") != "READY_FOR_GUARDED_SUBMISSION"
            or len(protection) != 1
            or protection[0][0].get("status")
            in {"SENDING", "UNKNOWN", "ACK_RECEIVED", "REJECTED"}
        ):
            raise ValueError("TRADE_NOTIFICATION_EVIDENCE_PENDING")
        protected = protection[0][0]
        common = {
            "account_id": self.scope.account_id,
            "symbol": trace["request"]["symbol"],
            "direction": plan["side"],
            "strategy": plan["strategy"],
            "system_tag": plan["system_tag"],
            "event_type": plan["event_type"],
            "score": plan["score"],
            "leverage": plan["leverage"],
            "margin_type": plan["margin_mode"],
            "quantity": _exact(quantity),
            "episode_id": episode,
            "signal_id": trace["signal"]["signal_id"],
        }
        return (
            trace,
            plan,
            sizing,
            opening,
            open_fills,
            entry,
            notional,
            readiness[0],
            protected,
            order_identity[0],
            common,
        )

    def _opening(self, episode):
        (
            trace,
            _,
            sizing,
            opening,
            fills,
            entry,
            notional,
            readiness,
            protection,
            exchange_order,
            common,
        ) = self._facts(episode)
        return {
            **common,
            "entry_price": _exact(entry),
            "opening_notional": _exact(notional),
            "planned_notional": sizing["notional"],
            "planned_loss": sizing["planned_loss"],
            "stop_price": sizing["stop_price"],
            "stop_status": protection["status"],
            "stop_algo_id": str(
                (protection.get("observation") or {}).get("algoId", "PENDING")
            ),
            "fees": _fees(fills),
            "opened_at_ms": min(row["occurred_at_ms"] for row in fills),
            "order_id": opening["order_id"],
            "client_order_id": opening["client_order_id"],
            "exchange_order_id": str(exchange_order),
            "analysis_reason": trace["decision"]["features"]["evaluation"]["analysis"][
                "reason"
            ],
            "required_margin": str(readiness["required_margin"]),
        }

    def _closing(self, episode):
        trace, _, sizing, _, open_fills, entry, notional, _, protection, _, common = (
            self._facts(episode)
        )
        if trace["episode"]["status"] != "SETTLED" or len(trace["settlements"]) != 1:
            raise ValueError("SETTLED_NOTIFICATION_EVIDENCE_REQUIRED")
        close_fills, closed, closing, _ = _average(trace, "CLOSE")
        if closed != amount(common["quantity"], positive=True):
            raise ValueError("CLOSE_QUANTITY_MISMATCH")
        with self.connect() as conn:
            outcome = conn.execute(
                """SELECT net_pnl::text,return_pct::text,quality_score::text,
                closing_price::text,settlement_revision FROM v2_directional_outcomes
                WHERE episode_id=%s""",
                (episode,),
            ).fetchone()
            exit_state = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope->>'namespace'='directional-exit-v1' AND scope->>'key'=%s""",
                (episode,),
            ).fetchone()
            order_rows = conn.execute(
                """SELECT order_id::text,exchange_order_id,request_evidence
                FROM v2_orders WHERE episode_id=%s AND leg='CLOSE' ORDER BY updated_at,order_id""",
                (episode,),
            ).fetchall()
        if outcome is None or not order_rows:
            raise ValueError("DIRECTIONAL_OUTCOME_PENDING")
        report = Ledger(self.connect).report(episode, settlement_currency="USDT")
        if report["accounting_status"] != "SETTLED":
            raise ValueError("FINAL_ACCOUNTING_REQUIRED")
        reasons = [row[2].get("reason") for row in order_rows if row[2].get("reason")]
        native = any(row[2].get("origin") == "BINANCE_ALGO_CHILD" for row in order_rows)
        if native:
            reason = "NATIVE_" + protection["spec"]["kind"]
        elif exit_state is not None:
            reason = exit_state[0]["decision"]["reason"]
        elif reasons:
            reason = ",".join(dict.fromkeys(reasons))
        else:
            reason = "RECONCILED_EXTERNAL_CLOSE"
        opened_at = min(row["occurred_at_ms"] for row in open_fills)
        closed_at = max(row["occurred_at_ms"] for row in close_fills)
        with localcontext() as context:
            context.prec = 100
            risk_multiple = Decimal(report["net_pnl"]) / amount(
                sizing["planned_loss"], positive=True
            )
        return {
            **common,
            "entry_price": _exact(entry),
            "closing_price": _exact(closing),
            "opening_notional": _exact(notional),
            "gross_pnl": report["gross_pnl"],
            "fees": report["fees"],
            "funding": report["cash_adjustments"],
            "net_pnl": outcome[0],
            "return_pct": outcome[1],
            "risk_multiple": _exact(risk_multiple),
            "exit_reason": reason,
            "held_ms": closed_at - opened_at,
            "opened_at_ms": opened_at,
            "closed_at_ms": closed_at,
            "close_order_ids": ",".join(row[0] for row in order_rows),
            "exchange_order_ids": ",".join(str(row[1]) for row in order_rows),
            "settlement_revision": outcome[4],
            "stop_status": protection["status"],
        }

    def _deliver(self, event_id, episode, kind, _payload):
        if kind.startswith("ORDER_STATE:"):
            return self.sink(
                event_id, "SANDBOX", "TRADE_OPENED", self._opening(episode)
            )
        if kind.startswith("SETTLED:"):
            return self.sink(
                event_id, "SANDBOX", "TRADE_CLOSED", self._closing(episode)
            )
        return True
