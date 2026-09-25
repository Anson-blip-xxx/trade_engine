"""Durable, read-only Telegram projections for confirmed trade lifecycle facts."""

import json
import time
from dataclasses import asdict
from decimal import Decimal, localcontext

from v2_core.account_risk import AccountScope
from v2_core.delivery import Projector
from v2_core.ledger import Ledger, amount
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
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
    def __init__(self, connect, sink, *, scope, pin_messages=False):
        if (
            not isinstance(scope, AccountScope)
            or scope.environment != "SANDBOX"
            or not isinstance(sink, TelegramOperationalSink)
            or sink.environment != scope.environment
        ):
            raise ValueError("account-bound Testnet trade notifications required")
        self.connect, self.sink, self.scope = connect, sink, scope
        if type(pin_messages) is not bool:
            raise ValueError("explicit pin flag required")
        self.pin_messages = pin_messages
        self.store = BusinessState(connect)
        self.data = TradingData(connect)
        self.projector = TradeLifecycleProjector(
            connect,
            "trade-telegram-v1:" + scope.key,
            self._deliver,
            scope=scope,
        )

    def run_once(self, limit=10):
        # Pin I/O runs in the independent watchdog, never the trading cycle.
        return self.projector.run_scheduled_batch(limit, lease_seconds=30)

    def _pin_key(self, event_id):
        return StateKey(**asdict(self.scope), namespace="telegram-pin-v1", key=event_id)

    def _queue_pin(self, event_id, message_id):
        key = self._pin_key(event_id)
        previous = self.store.read(key)
        if previous:
            return
        result = self.store.change(
            key,
            expected_version=0,
            request_key="message-confirmed",
            payload={
                "message_id": message_id,
                "destination": self.sink.pin_destination,
                "status": "PENDING",
                "attempts": 0,
                "next_attempt_ms": 0,
            },
            reason="TELEGRAM_TRADE_MESSAGE_CONFIRMED",
        )
        if result.code != "APPLIED":
            raise ValueError("TELEGRAM_RECEIPT_CONFLICT")

    def _retry_pins(self, limit):
        now = time.time_ns() // 1000000
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT scope->>'key' FROM v2_business_state
                WHERE scope @> %s::jsonb AND NOT deleted AND payload->>'status'='PENDING'
                AND (payload->>'next_attempt_ms')::bigint<=%s
                ORDER BY (payload->>'next_attempt_ms')::bigint,state_id LIMIT %s""",
                (
                    json.dumps({**asdict(self.scope), "namespace": "telegram-pin-v1"}),
                    now,
                    limit,
                ),
            ).fetchall()
        for (event_id,) in rows:
            key = self._pin_key(event_id)
            saved = self.store.read(key)
            payload = json.loads(saved.payload_json)
            if payload["status"] != "PENDING":
                continue
            if payload["destination"] != self.sink.pin_destination:
                payload.update(
                    status="DESTINATION_CHANGED", error="PIN_DESTINATION_CHANGED"
                )
            else:
                try:
                    self.sink.pin(payload["message_id"])
                    payload.update(status="PINNED", error=None)
                except Exception:  # noqa: BLE001 - independent durable pin retry
                    payload.update(error="PIN_UNCONFIRMED")
                payload["attempts"] += 1
                payload["next_attempt_ms"] = now + min(
                    3600000, 60000 * 2 ** min(payload["attempts"] - 1, 6)
                )
            self.store.change(
                key,
                expected_version=saved.version,
                request_key=f"pin:{saved.version}",
                payload=payload,
                reason="TELEGRAM_PIN_ATTEMPT",
            )

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
            "symbol": trace["request"]["symbol"],
            "direction": plan["side"],
            "strategy": plan["strategy"],
            "system_tag": plan["system_tag"],
            "event_type": plan["event_type"],
            "score": plan["score"],
            "leverage": plan["leverage"],
            "margin_type": plan["margin_mode"],
            "quantity": _exact(quantity),
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
            common,
        )

    def _opening(self, episode):
        (
            trace,
            _,
            sizing,
            _opening,
            fills,
            entry,
            notional,
            readiness,
            protection,
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
            "fees": _fees(fills),
            "opened_at_ms": min(row["occurred_at_ms"] for row in fills),
            "analysis_reason": trace["decision"]["features"]["evaluation"]["analysis"][
                "reason"
            ],
            "required_margin": str(readiness["required_margin"]),
        }

    def _closing(self, episode):
        trace, _, sizing, _, open_fills, entry, notional, _, protection, common = (
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
                """SELECT request_evidence
                FROM v2_orders WHERE episode_id=%s AND leg='CLOSE' ORDER BY updated_at,order_id""",
                (episode,),
            ).fetchall()
        if outcome is None or not order_rows:
            raise ValueError("DIRECTIONAL_OUTCOME_PENDING")
        report = Ledger(self.connect).report(episode, settlement_currency="USDT")
        if report["accounting_status"] != "SETTLED":
            raise ValueError("FINAL_ACCOUNTING_REQUIRED")
        reasons = [row[0].get("reason") for row in order_rows if row[0].get("reason")]
        native = any(row[0].get("origin") == "BINANCE_ALGO_CHILD" for row in order_rows)
        if any(
            row[0].get("origin") == "APPROVED_TESTNET_MAINTENANCE" for row in order_rows
        ):
            reason = "USER_APPROVED_MAINTENANCE（授权维护平仓，非策略自动退出）"
        elif native:
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
            "stop_status": protection["status"],
        }

    def _deliver(self, event_id, episode, kind, _payload):
        if self.pin_messages and self.store.read(self._pin_key(event_id)):
            return True  # Confirmed send survives projector-ACK failure; pin independently.
        options = (
            {"receipt": lambda mid: self._queue_pin(event_id, mid)}
            if self.pin_messages
            else {}
        )
        if kind.startswith("ORDER_STATE:"):
            return self.sink(
                event_id, "SANDBOX", "TRADE_OPENED", self._opening(episode), **options
            )
        if kind.startswith("SETTLED:"):
            return self.sink(
                event_id, "SANDBOX", "TRADE_CLOSED", self._closing(episode), **options
            )
        return True
