"""Causal receipt -> actual signed-protocol OPEN -> management -> settlement.

All HTTP responses are simulated; PG is isolated. No manual OPEN fill or fake
opening-readiness baseline is inserted. This is not an exchange soak test.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
from threading import Event
from urllib.parse import parse_qs, urlsplit

import pytest
from test_v2_account_inventory import TelegramConnection, sink

from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_strategy_entry_job import run_entry_work
from services.v2_strategy_runtime_job import _dispatch_runtime
from tests.v2_core.test_v2_strategy_candidate_producer import (
    database,
    fenced,
    financial,
    scene,
)
from tests.v2_core.test_v2_strategy_exit import CONFIG, REGISTRY
from tests.v2_core.test_v2_strategy_opening_gate import OpeningVenue
from tests.v2_core.test_v2_strategy_protection import position
from v2_core.account_risk import RiskReference
from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey
from v2_core.strategy_deployment import StrategyDeployments
from v2_core.strategy_plan_store import read_trade_plan
from v2_core.trade_notifications import TradeLifecycleNotifications

__all__ = ["database", "fenced"]


class Wire:
    def __init__(self, venue, *, host, timeout):
        assert host == "demo-fapi.binance.com" and timeout == 3
        self.venue = venue

    def request(self, method, target, body=None, headers=None):
        parsed = urlsplit(target)
        fields = parse_qs(body if body is not None else parsed.query)
        assert all(len(value) == 1 for value in fields.values())
        params = {
            key: value[0]
            for key, value in fields.items()
            if key not in {"signature", "timestamp", "recvWindow"}
        }
        for key in ("orderId", "leverage", "limit", "startTime", "endTime"):
            if key in params:
                params[key] = int(params[key])
        self.response = self.venue(method, parsed.path, params)

    def getresponse(self):
        response = self.response

        class Response:
            status = 200

            def read(self, limit):
                return json.dumps(response).encode()[:limit]

            def getheader(self, *_args):
                return None

        return Response()

    def close(self):
        pass


class Public:
    environment = "SANDBOX"

    def __init__(self, plan, catalog):
        self.plan, self.catalog = plan, catalog

    def __call__(self, path, params):
        if path == "/fapi/v1/premiumIndex":
            return {
                "symbol": self.plan.symbol,
                "markPrice": self.plan.entry_price,
                "time": financial.now(),
            }
        assert path == "/fapi/v1/exchangeInfo"
        return {"symbols": [self.catalog]}


@pytest.mark.parametrize(
    "side,source,lost", [("BUY", "tv_bridge", False), ("SELL", "s3", True)]
)
@pytest.mark.parametrize("real_provider", [False, True])
def test_same_generated_plan_from_signal_to_cash_settlement(
    fenced, monkeypatch, side, source, lost, real_provider
):
    with monkeypatch.context() as provider_patch:
        if real_provider:
            from tests.v2_core.test_v2_strategy_real_producer import composition

            real = composition(fenced, provider_patch, side=side, source=source)
            selected = real.selected
            selected.producer, selected.deployment = real.producer, real.deployment
            registry = real.config.entry.registry()
        else:
            provider_patch.setattr(financial, "REGISTRY", REGISTRY)
            selected = scene(fenced, side=side, source=source)
            registry = REGISTRY
        configuration = selected.deployment.config
        configuration["producer"]["exit_config"] = {**CONFIG, "max_holding_ms": 1000}
        # Publish the explicit exit contract before making any candidate.
        deployment = replace(
            selected.deployment, config_json=canonical(configuration), generation=2
        )
        StrategyDeployments(fenced.admin, registry).publish(
            deployment, expected_generation=1, request_id="cycle-exit-contract"
        )
        if real_provider:
            assert selected.producer.prepare(selected.identity)["status"] == "FROZEN"
    policy = PolicyStore(fenced.admin, financial.SCOPE)
    policy.patch(
        {"settlement.cash_basis_version": 2},
        expected_version=policy.read().version,
        reason="isolated complete typed cycle",
    )
    consumed = run_entry_work(
        fenced.admin,
        binding=selected.binding,
        registry=registry,
        producer=deployment.instance_id,
        source=source,
        stop=Event(),
        clock_ms=financial.now,
        candidate_producer=selected.producer,
    )
    assert consumed["signals"] == {selected.identity: "PREPARED"}, consumed
    state = selected.producer._state(selected.identity)
    episode = state.receipt.intent_id
    with fenced.admin() as conn:
        plan = read_trade_plan(conn, episode)
        opening = conn.execute(
            "SELECT order_id::text FROM v2_orders WHERE episode_id=%s", (episode,)
        ).fetchone()[0]
    venue = OpeningVenue()
    venue.account_id = plan.scope.account_id
    initial_wallet = Decimal(2000 if real_provider else 1000)
    venue.wallet(str(initial_wallet))
    venue.lose_opening_response = lost
    venue.rows["/fapi/v1/symbolConfig"][0]["leverage"] = plan.leverage
    runtime = _dispatch_runtime(
        fenced.admin,
        binding=selected.binding,
        guard=selected.producer.guard,
        private={"api_key": "qa-key", "api_secret": "qa-secret"},
        public=Public(plan, selected.catalog),
        reader=None,
        stop=Event(),
        signed_connection_factory=lambda host, timeout: Wire(
            venue, host=host, timeout=timeout
        ),
        risk_reference=lambda order: RiskReference(
            "SANDBOX", order["symbol"], "qa", plan.entry_price, financial.now()
        ),
    )
    outcome = runtime.execution.dispatch(opening)
    assert outcome == ("UNKNOWN" if lost else "ACKNOWLEDGED"), (
        outcome,
        runtime.data.trace(episode),
    )
    assert len(venue.opening_posts) == 1
    baseline = json.loads(
        BusinessState(fenced.admin)
        .read(
            StateKey(
                **asdict(plan.scope), namespace="opening-readiness-v1", key=opening
            )
        )
        .payload_json
    )
    assert baseline["status"] == "READY_FOR_GUARDED_SUBMISSION"
    opened_at = max(financial.now(), baseline["inventory"]["finished_at_ms"] + 1)
    venue.opening.update(status="FILLED", executedQty=plan.quantity)
    venue.opening_fills = [
        {
            "id": 1,
            "orderId": 10,
            "symbol": plan.symbol,
            "side": side,
            "positionSide": "BOTH",
            "qty": plan.quantity,
            "price": plan.entry_price,
            "commission": ".001",
            "commissionAsset": "USDT",
            "time": opened_at,
            "realizedPnl": "0",
        }
    ]
    position(venue, side=side, quantity=plan.quantity)
    # Opening adapter is never used for management; independent management ports
    # can recover the original order even after the opening deployment is OFF.
    StrategyDeployments(fenced.admin, registry).publish(
        replace(deployment, generation=3, mode="OFF"),
        expected_generation=2,
        request_id="cycle-disable-opening",
    )
    adapter = BinanceFutures(
        venue, account_id=financial.SCOPE.account_id, environment="SANDBOX"
    )
    tick = opened_at + 300000
    management = DataRuntime(
        fenced.admin,
        scope=financial.SCOPE,
        submit=adapter.submit,
        query=adapter.query,
        clock_ms=lambda: tick,
        risk_check=lambda order: order["leg"] == "CLOSE",
    )
    assert management.execution.recover(opening) == "FILLED"

    def protection_stage(stamp):
        return DirectionalProtectionStage(
            fenced.admin,
            venue,
            scope=plan.scope,
            reference=lambda symbol: {
                "symbol": symbol,
                "environment": "SANDBOX",
                "mark_price": plan.entry_price,
                "tick_size": next(
                    item["tickSize"]
                    for item in selected.catalog["filters"]
                    if item["filterType"] == "PRICE_FILTER"
                ),
                "observed_at_ms": stamp,
            },
            clock_ms=lambda: stamp,
            allow_writes=True,
        )

    protected = protection_stage(tick).run_once()
    assert protected["stops"][opening]["status"] == "NEW", json.dumps(protected)
    assert venue.writes[0]["triggerPrice"] == plan.stop_price
    messages = TelegramConnection()
    notifications = TradeLifecycleNotifications(
        fenced.admin, sink(messages), scope=plan.scope
    )
    opened_notification = notifications.run_once()
    assert (
        opened_notification["delivered"] == 1 and opened_notification["failed"] == 0
    ), opened_notification
    close_price = str(
        Decimal(plan.entry_price) + (Decimal(".1") if side == "BUY" else Decimal("-.1"))
    )
    exits = DirectionalExitStage(
        fenced.admin,
        venue,
        runtime=management,
        scope=financial.SCOPE,
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
    closed_at = tick + 1
    pnl = Decimal(plan.quantity) * Decimal(".1")
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
            "commission": ".001",
            "commissionAsset": "USDT",
            "time": closed_at,
            "realizedPnl": str(pnl),
        }
    ]
    assert management.execution.recover(reduction["order_id"]) == "FILLED"
    venue.rows["/fapi/v3/positionRisk"] = []
    assert protection_stage(closed_at + 1).run_once()["status"] == "CLEAR"
    net = pnl - Decimal(".002")
    venue.wallet(str(initial_wallet + net))
    venue.income = [
        {
            "symbol": plan.symbol,
            "asset": "USDT",
            "incomeType": kind,
            "income": value,
            "tradeId": trade,
            "tranId": identity,
            "time": stamp,
        }
        for kind, value, trade, identity, stamp in (
            ("COMMISSION", "-.001", "1", 1, opened_at),
            ("COMMISSION", "-.001", "901", 2, closed_at),
            ("REALIZED_PNL", str(pnl), "901", 3, closed_at),
        )
    ]
    settlement = DirectionalSettlementStage(
        fenced.admin,
        venue,
        scope=financial.SCOPE,
        clock_ms=lambda: closed_at + 70000,
        commit_guard=selected.producer.guard.require_transaction,
    )
    final = settlement.run_once()
    assert final["status"] == "CLEAR", final
    assert Decimal(final["settlements"][episode]["net_pnl"]) == net
    assert final["outcomes"][episode]["plan_digest"] == plan.plan_digest
    assert management.data.trace(episode)["account_risk"]["status"] == "RELEASED"
    closed_notification = notifications.run_once()
    assert (
        closed_notification["delivered"] == 1 and closed_notification["failed"] == 0
    ), closed_notification
    rendered = [json.loads(call[1]["body"])["text"] for call in messages.calls]
    assert len(rendered) == 2 and all("无传统评分" in text for text in rendered)
    assert "开仓成功" in rendered[0] and "平仓" in rendered[1]
    assert notifications.run_once()["delivered"] == 0
    saved = deepcopy(management.data.trace(episode))
    assert (
        run_entry_work(
            fenced.admin,
            binding=selected.binding,
            registry=registry,
            producer=deployment.instance_id,
            source=source,
            stop=Event(),
            clock_ms=financial.now,
            candidate_producer=selected.producer,
        )["signals"]
        == {}
    )
    assert management.data.trace(episode) == saved
    assert len(venue.opening_posts) == 1
