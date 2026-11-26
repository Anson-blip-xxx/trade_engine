"""Durable, read-only Telegram projections for confirmed trade lifecycle facts."""

import json
import time
from dataclasses import asdict
from decimal import Decimal, localcontext

from v2_core.account_risk import AccountScope
from v2_core.delivery import Projector
from v2_core.directional_outcomes import exact, settlement_amount
from v2_core.evidence import canonical, digest
from v2_core.ledger import Ledger, amount
from v2_core.precision import decimal_text, money_context
from v2_core.protection import ProtectionSpec
from v2_core.protection_supervisor import external_flat_result
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_lifecycle import (
    LIFECYCLE_EPISODE_SQL,
    STRATEGY_OUTCOME_SCHEMA,
    plan_provenance,
    strategy_plan_from_trace,
    validate_settlement_provenance,
)
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.telegram import TelegramOperationalSink


def _exact(value):
    return decimal_text(Decimal(value))


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


def _require_typed_opening_protection(trace, protection):
    """A historical fill is not proof of current protection for live exposure."""
    legs = {row["order_id"]: row["leg"] for row in trace["orders"]}
    with money_context():
        remaining = sum(
            (
                amount(row["quantity"], positive=True)
                * (1 if legs[row["order_id"]] == "OPEN" else -1)
                for row in trace["fills"]
            ),
            Decimal(0),
        )
    if (
        remaining < 0
        or (remaining > 0 and protection.get("status") != "NEW")
        or (remaining == 0 and trace["episode"]["status"] != "SETTLED")
    ):
        raise ValueError("STRATEGY_OPEN_NOTIFICATION_PROTECTION_PENDING")


class TradeLifecycleProjector(Projector):
    def _scope_filter(self):
        scoped, params = super()._scope_filter()
        return (
            f"""({scoped}) AND EXISTS (
                SELECT 1 FROM v2_trade_intents i WHERE i.intent_id=e.intent_id
                AND {LIFECYCLE_EPISODE_SQL}) AND (
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
        if trace is None or any(
            trace["scope"].get(key) != value
            for key, value in asdict(self.scope).items()
        ):
            raise ValueError("DIRECTIONAL_NOTIFICATION_SCOPE_MISMATCH")
        typed = strategy_plan_from_trace(trace)
        if typed is None:
            if (
                trace["scope"]["producer"] not in {"s6", "s8"}
                or trace["signal"] is None
            ):
                raise ValueError("DIRECTIONAL_NOTIFICATION_SCOPE_MISMATCH")
            evaluation = trace["decision"]["features"]["evaluation"]
            plan, sizing = evaluation["market_plan"], evaluation["sizing"]
        else:
            plan = {
                "side": "LONG" if typed.side == "BUY" else "SHORT",
                "strategy": typed.strategy_id,
                "system_tag": typed.code_version,
                "event_type": "TYPED_PLAN",
                "score": "不适用（无传统评分）",
                "leverage": typed.leverage,
                "margin_mode": typed.margin_mode,
            }
            with money_context():
                sizing = {
                    "notional": _exact(
                        amount(typed.quantity) * amount(typed.entry_price)
                    ),
                    "planned_loss": typed.planned_loss,
                    "stop_price": typed.stop_price,
                }
        openings = [row for row in trace["orders"] if row["leg"] == "OPEN"]
        if len(openings) != 1:
            raise ValueError("SINGLE_OPENING_ORDER_REQUIRED")
        opening = openings[0]
        open_fills, quantity, entry, notional = _average(trace, "OPEN")
        with self.connect() as conn:
            if typed is not None and read_trade_plan(conn, episode) != typed:
                raise ValueError("STRATEGY_NOTIFICATION_PLAN_MISMATCH")
            order_identity = conn.execute(
                "SELECT exchange_order_id FROM v2_orders WHERE order_id=%s",
                (opening["order_id"],),
            ).fetchone()
            readiness = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope @> %s::jsonb
                AND scope->>'namespace'='opening-readiness-v1' AND scope->>'key'=%s""",
                (json.dumps(asdict(self.scope)), opening["order_id"]),
            ).fetchone()
            protection = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope @> %s::jsonb
                AND scope->>'namespace'='testnet-protection-v1'
                AND payload->'spec'->>'episode'=%s LIMIT 2""",
                (json.dumps(asdict(self.scope)), episode),
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
        if typed is not None and (
            readiness[0].get("account_scope") != asdict(typed.scope)
            or readiness[0].get("symbol") != typed.symbol
            or readiness[0].get("leverage") != typed.leverage
            or readiness[0].get("margin_type") != typed.margin_mode
            or amount(readiness[0].get("quantity"), positive=True)
            != amount(typed.quantity)
            or quantity > amount(typed.quantity)
            or protection[0][0].get("status")
            not in {
                "NEW",
                "CANCELED",
                "TRIGGERING",
                "TRIGGERED",
                "FINISHED",
                "EXPIRED",
                "EXTERNAL_FLAT_RECONCILED",
            }
            or protection[0][0].get("spec", {}).get("symbol") != typed.symbol
            or protection[0][0].get("spec", {}).get("side") != typed.exit_side
        ):
            raise ValueError("STRATEGY_NOTIFICATION_READINESS_MISMATCH")
        protected = protection[0][0]
        if typed is not None and protected["status"] == "EXTERNAL_FLAT_RECONCILED":
            # This is NOT a venue cancel acknowledgement. Reuse the independent
            # original resolution/order/fill/flat-inventory proof before either
            # a settled historical opening or a closing may be announced.
            external_flat_result(
                self.connect,
                scope=self.scope,
                spec=ProtectionSpec(**protected["spec"]),
                payload=protected,
            )
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
        typed = strategy_plan_from_trace(trace)
        if typed is not None:
            _require_typed_opening_protection(trace, protection)
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
            "analysis_reason": (
                (
                    "历史开仓补发：该订单已平仓结算；"
                    if trace["episode"]["status"] == "SETTLED"
                    else ""
                )
                + "已验证冻结策略计划；无传统评分，不代表盈利保证"
                if typed is not None
                else trace["decision"]["features"]["evaluation"]["analysis"]["reason"]
            ),
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
        typed = strategy_plan_from_trace(trace)
        with self.connect() as conn:
            outcome = (
                None
                if typed is not None
                else conn.execute(
                    """SELECT net_pnl::text,return_pct::text,quality_score::text,
                closing_price::text,settlement_revision FROM v2_directional_outcomes
                WHERE episode_id=%s""",
                    (episode,),
                ).fetchone()
            )
            exit_state = conn.execute(
                """SELECT payload FROM v2_business_state WHERE NOT deleted
                AND scope @> %s::jsonb
                AND scope->>'namespace'='directional-exit-v1' AND scope->>'key'=%s""",
                (json.dumps(asdict(self.scope)), episode),
            ).fetchone()
            order_rows = conn.execute(
                """SELECT request_evidence
                FROM v2_orders WHERE episode_id=%s AND leg='CLOSE' ORDER BY updated_at,order_id""",
                (episode,),
            ).fetchall()
        if (typed is None and outcome is None) or not order_rows:
            raise ValueError("DIRECTIONAL_OUTCOME_PENDING")
        report = Ledger(self.connect).report(episode, settlement_currency="USDT")
        if report["accounting_status"] != "SETTLED":
            raise ValueError("FINAL_ACCOUNTING_REQUIRED")
        if typed is not None:
            outcome = self._typed_result(
                episode,
                typed,
                trace,
                report,
                open_fills,
                close_fills,
                notional,
                closing,
            )
            if exit_state is not None and (
                exit_state[0].get("source") != "strategy-exit-v1"
                or exit_state[0].get("account_scope") != asdict(self.scope)
                or exit_state[0].get("strategy_plan") != plan_provenance(typed)
            ):
                raise ValueError("STRATEGY_NOTIFICATION_EXIT_MISMATCH")
        reasons = [row[0].get("reason") for row in order_rows if row[0].get("reason")]
        native = any(row[0].get("origin") == "BINANCE_ALGO_CHILD" for row in order_rows)
        if any(
            row[0].get("origin") == "BINANCE_EXTERNAL_CLOSE"
            and row[0].get("kind") == "LIQUIDATION"
            for row in order_rows
        ):
            reason = "EXCHANGE_LIQUIDATION（交易所强制平仓）"
        elif "UNCONFIRMED_PROTECTION" in reasons:
            reason = "UNCONFIRMED_PROTECTION（止损保护未确认，应急减仓）"
        elif any(
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
            insurance_clear = sum(
                (
                    amount(row["amount"])
                    for row in trace["cash"]
                    if row["evidence"].get("income_type") == "INSURANCE_CLEAR"
                ),
                Decimal(0),
            )
            other_cash = amount(report["cash_adjustments"]) - insurance_clear
        return {
            **common,
            "entry_price": _exact(entry),
            "closing_price": _exact(closing),
            "opening_notional": _exact(notional),
            "gross_pnl": report["gross_pnl"],
            "fees": report["fees"],
            "funding": _exact(other_cash),
            **({"insurance_clear": _exact(insurance_clear)} if insurance_clear else {}),
            "net_pnl": outcome[0],
            "return_pct": outcome[1],
            "risk_multiple": _exact(risk_multiple),
            "exit_reason": reason,
            "held_ms": closed_at - opened_at,
            "opened_at_ms": opened_at,
            "closed_at_ms": closed_at,
            "stop_status": protection["status"],
        }

    def _typed_result(
        self, episode, plan, trace, report, opened, closed, notional, price
    ):
        """Read-only accounting proof; optional research cannot supply the PnL."""
        settlement = trace["settlements"][0]
        proof = settlement["evidence"]
        validate_settlement_provenance(plan, proof)
        if (
            settlement["currency"] != "USDT"
            or settlement["revision"] != trace["episode"]["accounting_revision"]
            or settlement["revision"] != report["accounting_revision"]
            or settlement_amount(proof["net_pnl"])
            != settlement_amount(report["net_pnl"])
        ):
            raise ValueError("STRATEGY_NOTIFICATION_SETTLEMENT_MISMATCH")
        with money_context():
            net = settlement_amount(report["net_pnl"])
            return_pct = exact(net / notional * 100)
        key = StateKey(
            **asdict(self.scope), namespace=STRATEGY_OUTCOME_SCHEMA, key=episode
        )
        saved = self.store.read(key)
        if saved is not None:
            payload = json.loads(saved.payload_json)
            with self.connect() as conn:
                history = conn.execute(
                    "SELECT payload,deleted,reason FROM v2_state_history WHERE state_id=%s AND version=1",
                    (key.identity,),
                ).fetchone()
            expected = {
                "episode_id": episode,
                "source": STRATEGY_OUTCOME_SCHEMA,
                "outcome_schema": STRATEGY_OUTCOME_SCHEMA,
                "account_scope": asdict(self.scope),
                "strategy_plan": plan_provenance(plan),
                "plan_digest": plan.plan_digest,
                "symbol": plan.symbol,
                "side": plan.side,
                "settlement_revision": settlement["revision"],
                "settlement_digest": digest(canonical(proof)),
                "net_pnl": exact(net),
                "return_pct": return_pct,
                "closing_price": exact(price),
                "opened_at_ms": min(row["occurred_at_ms"] for row in opened),
                "closed_at_ms": max(row["occurred_at_ms"] for row in closed),
                "closing_price_basis": "ALL_CLOSE_FILL_VWAP_NOT_FINAL_FLAT_MARK",
            }
            fill_orders = {row["order_id"] for row in closed}
            requests = [
                row["request_evidence"]
                for row in sorted(trace["orders"], key=lambda row: row["order_id"])
                if row["leg"] == "CLOSE" and row["order_id"] in fill_orders
            ]
            with money_context():
                expected.update(
                    planned_quantity=plan.quantity,
                    planned_notional=exact(
                        amount(plan.quantity) * amount(plan.entry_price)
                    ),
                    opening_quantity=exact(
                        sum((amount(row["quantity"]) for row in opened), Decimal(0))
                    ),
                    opening_notional=exact(notional),
                    closing_quantity=exact(
                        sum((amount(row["quantity"]) for row in closed), Decimal(0))
                    ),
                    holding_duration_ms=expected["closed_at_ms"]
                    - expected["opened_at_ms"],
                    gross_pnl=exact(Decimal(report["gross_pnl"])),
                    fees=exact(Decimal(report["fees"])),
                    cash_adjustments=exact(Decimal(report["cash_adjustments"])),
                    exit_evidence=requests,
                    strategy_learning_eligible=not any(
                        row.get("origin") == "APPROVED_TESTNET_MAINTENANCE"
                        for row in requests
                    ),
                )
            if (
                saved.deleted
                or saved.version != 1
                or history != (payload, False, "VERIFIED_STRATEGY_OUTCOME")
                or payload != expected
            ):
                raise ValueError("STRATEGY_NOTIFICATION_OUTCOME_MISMATCH")
        return exact(net), return_pct

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
