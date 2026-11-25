"""Immutable settlement-derived outcomes without fabricated legacy scores.

Outcome failure is analysis debt, never a rollback of committed settlement.
The wrapper only creates version 1 and verifies it against immutable history;
no operation here modifies orders, reservations, or financial accounting.
"""

import json
from contextlib import nullcontext
from dataclasses import asdict
from decimal import Decimal
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.directional_outcomes import exact, settlement_amount
from v2_core.evidence import canonical, digest
from v2_core.ledger import Ledger, amount
from v2_core.precision import money_context
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_lifecycle import (
    STRATEGY_OUTCOME_SCHEMA,
    plan_provenance,
    validate_settlement_provenance,
)
from v2_core.strategy_plan_store import read_trade_plan


class StrategyOutcomeJournal:
    def __init__(self, connect, *, scope):
        if type(scope) is not AccountScope:
            raise TypeError("typed account scope required")
        self.connect, self.scope = connect, scope

    def record(self, episode):
        episode = str(UUID(episode))
        with self.connect() as conn:
            conn.execute(
                "SELECT intent_id FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                (episode,),
            )
            plan = read_trade_plan(conn, episode)
            if plan is None or plan.scope != self.scope:
                raise ValueError("SETTLED_STRATEGY_EPISODE_REQUIRED")
            row = conn.execute(
                """SELECT s.revision,s.evidence,
                max(f.occurred_at_ms) FILTER(WHERE o.leg='CLOSE'),
                min(f.occurred_at_ms) FILTER(WHERE o.leg='OPEN'),
                sum(f.quantity*f.price) FILTER(WHERE o.leg='OPEN'),
                sum(f.quantity) FILTER(WHERE o.leg='OPEN'),
                sum(f.quantity*f.price) FILTER(WHERE o.leg='CLOSE'),
                sum(f.quantity) FILTER(WHERE o.leg='CLOSE')
                FROM v2_episodes ep JOIN v2_settlements s
                  ON s.episode_id=ep.episode_id AND s.revision=ep.accounting_revision
                JOIN v2_orders o ON o.episode_id=ep.episode_id
                JOIN v2_fills f USING(order_id)
                WHERE ep.episode_id=%s AND ep.status='SETTLED' AND s.currency='USDT'
                GROUP BY s.revision,s.evidence""",
                (episode,),
            ).fetchone()
            if row is None:
                raise ValueError("SETTLED_STRATEGY_EPISODE_REQUIRED")
            revision, settlement, closed, opened_at, opening, opened, closing, qty = row
            validate_settlement_provenance(plan, settlement)
            if (
                closed is None
                or opened_at is None
                or closed < opened_at
                or qty is None
                or not 0 < qty == opened <= amount(plan.quantity)
                or opening is None
                or opening <= 0
                or closing is None
                or closing <= 0
            ):
                raise ValueError("STRATEGY_OUTCOME_FACT_MISMATCH")
            report = Ledger(lambda: nullcontext(conn)).report(
                episode, settlement_currency="USDT", connection=conn
            )
            net = settlement_amount(settlement["net_pnl"])
            if (
                report["accounting_status"] != "SETTLED"
                or report["accounting_revision"] != revision
                or settlement_amount(report["net_pnl"]) != net
            ):
                raise ValueError("STRATEGY_OUTCOME_FACT_MISMATCH")
            with money_context():
                result = {
                    "episode_id": episode,
                    "closed_at_ms": closed,
                    "return_pct": exact(net / opening * 100),
                    "closing_price": exact(closing / qty),
                    "plan_digest": plan.plan_digest,
                    "outcome_schema": STRATEGY_OUTCOME_SCHEMA,
                }
                planned_notional = exact(
                    amount(plan.quantity) * amount(plan.entry_price)
                )
            requests = [
                r[0]
                for r in conn.execute(
                    """SELECT request_evidence FROM v2_orders o WHERE episode_id=%s
                AND leg='CLOSE' AND EXISTS (SELECT 1 FROM v2_fills f WHERE f.order_id=o.order_id)
                ORDER BY o.order_id""",
                    (episode,),
                ).fetchall()
            ]
            maintenance = any(
                r.get("origin") == "APPROVED_TESTNET_MAINTENANCE" for r in requests
            )
            payload = {
                **result,
                "source": STRATEGY_OUTCOME_SCHEMA,
                "account_scope": asdict(self.scope),
                "strategy_plan": plan_provenance(plan),
                "symbol": plan.symbol,
                "side": plan.side,
                "planned_quantity": plan.quantity,
                "planned_notional": planned_notional,
                "opening_quantity": exact(opened),
                "opening_notional": exact(opening),
                "closing_quantity": exact(qty),
                "opened_at_ms": opened_at,
                "holding_duration_ms": closed - opened_at,
                "closing_price_basis": "ALL_CLOSE_FILL_VWAP_NOT_FINAL_FLAT_MARK",
                "settlement_revision": revision,
                "settlement_digest": digest(canonical(settlement)),
                "gross_pnl": exact(Decimal(report["gross_pnl"])),
                "fees": exact(Decimal(report["fees"])),
                "cash_adjustments": exact(Decimal(report["cash_adjustments"])),
                "net_pnl": exact(net),
                "exit_evidence": requests,
                "strategy_learning_eligible": not maintenance,
            }
            key = StateKey(
                **asdict(self.scope), namespace=STRATEGY_OUTCOME_SCHEMA, key=episode
            )
            state = BusinessState(lambda: nullcontext(conn))
            saved = state.read(key)
            if saved is None:
                result_write = state.change(
                    key,
                    expected_version=0,
                    request_key="settlement:" + str(revision),
                    payload=payload,
                    reason="VERIFIED_STRATEGY_OUTCOME",
                )
                if result_write.code not in {"APPLIED", "ALREADY_APPLIED"}:
                    raise ValueError("STRATEGY_OUTCOME_CONFLICT")
                saved = state.read(key)
            history = conn.execute(
                """SELECT payload,deleted,reason FROM v2_state_history
                WHERE state_id=%s AND version=1""",
                (key.identity,),
            ).fetchone()
            if (
                saved is None
                or saved.version != 1
                or saved.deleted
                or json.loads(saved.payload_json) != payload
                or history != (payload, False, "VERIFIED_STRATEGY_OUTCOME")
            ):
                raise ValueError("STRATEGY_OUTCOME_CONFLICT")
            return result
