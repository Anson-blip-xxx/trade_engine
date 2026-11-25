"""One admitted plan traverses management to verified financial completion.

All exchange ports are in-memory fakes and PG is disposable. OPEN is an explicit
filled test fixture, not proof that the live/new-strategy OPEN transport has
been activated. Protective POST, reduce-only POST, fill recovery and native
flat cleanup use their actual service/adapter paths without deleting evidence.
"""

from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from uuid import uuid4

import pytest
from test_v2_strategy_deployment_execution import now
from test_v2_strategy_exit import CONFIG, REGISTRY, ExitVenue
from test_v2_strategy_protection import SCOPE, new_opening, position
from test_v2_strategy_protection import service as protection_stage

from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_trading_pipeline import _stage_clear
from tests.support.v2_admission import database as database_fixture
from v2_core.account_inventory import AccountInventory
from v2_core.account_risk import RiskReference
from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_lifecycle import plan_provenance

database = database_fixture


class CycleVenue(ExitVenue):
    def __init__(self):
        super().__init__()
        self.income, self.deletes = [], []
        self.wallet("1000")

    def wallet(self, value):
        self.rows["/fapi/v3/account"] = {
            "totalWalletBalance": value,
            "availableBalance": value,
            "totalMarginBalance": value,
        }

    def __call__(self, method, path, params):
        if path == "/fapi/v1/income":
            assert method == "GET"
            return deepcopy(self.income)
        if path == "/fapi/v1/algoOrder" and method == "DELETE":
            assert set(params) == {"algoId"}
            assert self.rows["/fapi/v3/positionRisk"] == []
            matching = [
                row
                for row in self.parents.values()
                if str(row["algoId"]) == str(params["algoId"])
            ]
            assert len(matching) == 1
            matching[0]["algoStatus"] = "CANCELED"
            self.rows["/fapi/v1/openAlgoOrders"] = [
                deepcopy(row)
                for row in self.parents.values()
                if row["algoStatus"] == "NEW"
            ]
            self.deletes.append(deepcopy(params))
            return {"code": 200, "msg": "success"}
        return super().__call__(method, path, params)


def baseline(database, venue, opening, opened_at):
    observed = AccountInventory(
        venue, scope=SCOPE, clock_ms=lambda: opened_at - 2
    ).collect(str(uuid4()))
    assert observed["status"] == "CLEAR_FOR_RECOVERY_CHECKS", observed
    assert observed["account_scope"] == asdict(SCOPE)
    BusinessState(database).change(
        StateKey(**asdict(SCOPE), namespace="opening-readiness-v1", key=opening),
        expected_version=0,
        request_key="qa-opening-baseline",
        reason="QA_OPENING_BASELINE",
        payload={
            "status": "READY_FOR_GUARDED_SUBMISSION",
            "blockers": [],
            "inventory": observed,
        },
    )


@pytest.mark.parametrize("side,close_price", [("BUY", "108"), ("SELL", "92")])
def test_same_plan_protection_exit_recovery_cash_settlement_and_risk_release(
    database, side, close_price
):
    selected, plan, episode, opening, data = new_opening(
        database,
        side=side,
        filled=None,
        registry=REGISTRY,
        plan_changes={
            "exit_config_json": canonical({**CONFIG, "max_holding_ms": 1000})
        },
    )
    policy = PolicyStore(database, SCOPE)
    policy.patch(
        {"settlement.cash_basis_version": 2},
        expected_version=policy.read().version,
        reason="QA typed cash basis",
    )
    venue = CycleVenue()
    opened_at = plan.decided_at_ms + 1
    baseline(database, venue, opening, opened_at)
    data.orders.transition(
        opening,
        expected_version=1,
        status="SUBMITTING",
        evidence={"source": "QA"},
        risk_reference=RiskReference("SANDBOX", plan.symbol, "qa", "100", now()),
    )
    data.ledger.record_fill(
        order_id=opening,
        exchange_fill_id="1",
        quantity=plan.quantity,
        price="100",
        fee="0.001",
        fee_currency="USDT",
        occurred_at_ms=opened_at,
        evidence={
            "source": "binance-userTrades",
            "trade_id": "1",
            "venue_realized_pnl": "0",
        },
    )
    data.orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        exchange_order_id="10",
        evidence={"fills_complete": True},
    )
    assert data.trace(episode)["account_risk"]["status"] == "HELD"
    position(venue, side=side, quantity=plan.quantity)
    tick = plan.decided_at_ms + 300000
    native = protection_stage(database, venue, tick=tick, allow_writes=True)
    protected = native.run_once()
    assert protected["status"] == "CLEAR", protected
    assert protected["stops"][opening]["status"] == "NEW"
    assert len(venue.writes) == 1
    assert venue.writes[0]["triggerPrice"] == plan.stop_price

    # Historical protection and reductions survive entry expiry and OFF.
    assert not plan.entry_valid_at(tick)
    StrategyDeployments(database, REGISTRY).publish(
        replace(selected, generation=2, mode="OFF"),
        expected_generation=1,
        request_id="qa-disable-entries",
    )
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runtime = DataRuntime(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: True,
        clock_ms=lambda: tick,
        scope=SCOPE,
    )
    exits = DirectionalExitStage(
        database,
        venue,
        runtime=runtime,
        scope=SCOPE,
        observe=lambda _: {
            "symbol": plan.symbol,
            "environment": "SANDBOX",
            "observed_at_ms": tick,
            "mark_price": close_price,
            "exit_fee_rate": ".0004",
        },
        clock_ms=lambda: tick,
        reference=lambda _: {},
        allow_writes=True,
    )
    reduction = exits.run_once()["exits"][episode]
    assert reduction["status"] == "DISPATCHED", reduction
    assert Decimal(reduction["quantity"]) == Decimal(plan.quantity)
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["reduceOnly"] == "true"
    assert venue.order_writes[0]["side"] == plan.exit_side
    assert venue.rows["/fapi/v1/openAlgoOrders"]  # Native protection remains installed.
    closing = reduction["order_id"]
    closed_at = tick + 1
    venue.close.update(status="FILLED", executedQty=plan.quantity)
    venue.trades = [
        {
            "id": 901,
            "orderId": 900,
            "symbol": plan.symbol,
            "side": plan.exit_side,
            "positionSide": "BOTH",
            "qty": plan.quantity,
            "price": close_price,
            "commission": "0.001",
            "commissionAsset": "USDT",
            "time": closed_at,
            "realizedPnl": "0.08",
        }
    ]
    assert runtime.execution.recover(closing) == "FILLED"
    trace = data.trace(episode)
    assert len(trace["fills"]) == 2
    assert trace["account_risk"]["status"] == "HELD"
    close_order = next(order for order in trace["orders"] if order["leg"] == "CLOSE")
    assert close_order["request_evidence"]["strategy_plan"] == plan_provenance(plan)

    venue.rows["/fapi/v3/positionRisk"] = []
    cleanup = protection_stage(
        database, venue, tick=closed_at + 1, allow_writes=True
    ).run_once()
    assert cleanup["status"] == "CLEAR", cleanup
    assert len(venue.deletes) == 1
    assert venue.rows["/fapi/v1/openAlgoOrders"] == []
    assert next(iter(venue.parents.values()))["algoStatus"] == "CANCELED"
    assert len(data.trace(episode)["fills"]) == 2

    net = Decimal(".078")
    venue.wallet(str(Decimal(1000) + net))
    venue.income = [
        {
            "symbol": plan.symbol,
            "asset": "USDT",
            "incomeType": kind,
            "income": value,
            "tradeId": trade,
            "tranId": number,
            "time": at,
        }
        for kind, value, trade, number, at in (
            ("COMMISSION", "-.001", "1", 1, opened_at),
            ("COMMISSION", "-.001", "901", 2, closed_at),
            ("REALIZED_PNL", ".08", "901", 3, closed_at),
        )
    ]
    settlement = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: closed_at + 70000
    )
    cash = settlement.cash.audit(episode)
    assert cash["status"] == "CASH_MATCHED", cash
    result = settlement.run_once()
    assert result["status"] == "CLEAR", result
    assert _stage_clear("settlement", settlement, result)
    assert Decimal(result["settlements"][episode]["net_pnl"]) == net
    assert result["outcomes"][episode]["plan_digest"] == plan.plan_digest
    assert result["outcomes"][episode]["outcome_schema"] == "strategy-outcome-v1"
    assert "quality_score" not in result["outcomes"][episode]
    assert data.trace(episode)["account_risk"]["status"] == "RELEASED"
    assert settlement.strategy_outcomes.record(episode) == result["outcomes"][episode]
    assert settlement.run_once()["settlements"] == {}
    assert len(venue.writes) == len(venue.order_writes) == len(venue.deletes) == 1
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_settlements WHERE episode_id=%s", (episode,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM v2_directional_outcomes"
        ).fetchone() == (0,)
