"""Real-PG component flow with fake SDK and fake Binance transport only.

The existing S3 fixture supplies synthetic market/account observations, not live
exchange collection. This exercises admission, durable JEV review, submission,
native stop/fill recovery, cash settlement and outcome recording together. It
does not validate network latency, the deployed daemon, profitability or TP
branches. The v2 QA network guard prohibits all Internet access.
"""

import time
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_directional_replay import SCOPE
from test_v2_jev_entry_context import model_response
from test_v2_trading_pipeline import case as pipeline_fixture
from test_v2_venue_readiness import Venue as ReadinessVenue

from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_jev_coordinator import JevCoordinator
from services.v2_jev_entry_context import JevEntryContext, policy_key
from services.v2_jev_reference import CONTROL_KEY, PUBLIC_SCOPE
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy, RiskReference
from v2_core.binance import BinanceFutures
from v2_core.jev_advisory import AdvisoryPolicy, JevAdvisory
from v2_core.jev_entry_gate import JevEntryGatePolicy
from v2_core.state import BusinessState
from v2_core.venue_readiness import GuardedOpeningSubmit, TestnetVenueReadiness

database = database_fixture
pipeline_case = pipeline_fixture


def prepare_candidate(database, pipeline_case):
    pipeline, _, account, _, _ = pipeline_case
    pipeline.market.run_once()  # Actual S3Runtime -> ProducerPublisher -> PG.
    with database() as conn:
        signal_id, signal = conn.execute(
            """SELECT signal_id::text,snapshot FROM v2_inbound_signals
            WHERE source='s3' AND snapshot->>'signal'='TREND_UP'"""
        ).fetchone()
    scheduler = pipeline.schedulers[0]
    worker, runtime = scheduler.worker, pipeline.runtime
    clock = SimpleNamespace(offset=0)
    clock.now = lambda: time.time_ns() // 1000000 + clock.offset
    runtime.clock_ms = clock.now
    runtime.account_risk.configure(
        SCOPE,
        AccountPolicy("USDT", "10000", 10, 0, "test-mark", 60000),
        expected_version=0,
    )
    scheduler.context_provider.clock = clock.now
    scheduler.context_provider.market.clock_ms = clock.now
    queued, sdk_calls, evaluations = [], [], []
    reference = SimpleNamespace(submit=lambda value: queued.append(deepcopy(value)))
    provider_policy = AdvisoryPolicy(enabled=True)
    for key, value in (
        (CONTROL_KEY, provider_policy),
        (policy_key(SCOPE), JevEntryGatePolicy(enabled=True, max_wait_ms=30000)),
    ):
        assert (
            BusinessState(database)
            .change(
                key,
                expected_version=0,
                request_key=str(uuid4()),
                payload=asdict(value),
                reason="isolated QA only",
            )
            .code
            == "APPLIED"
        )
    worker.entry_filter = JevEntryContext(database, reference, scope=SCOPE)
    original_decide = worker.decide

    def decide(signal, context, config):
        result = original_decide(signal, context, config)
        evaluations.append((deepcopy(context), result))
        return result

    worker.decide = decide

    def context():
        account.update(observed_at_ms=clock.now(), valid_until_ms=clock.now() + 60000)
        account["evidence"]["qa_refresh"] = len(evaluations)
        return scheduler.context_provider(signal)

    first = worker.consume(signal_id, context=context())
    assert first["status"] == "DEFERRED", first
    assert len(evaluations) == len(queued) == 1
    assert evaluations[0][1].action == "OPEN"
    assert worker.decision(signal_id) is None
    with database() as conn:
        for table in (
            "v2_strategy_decisions",
            "v2_trade_intents",
            "v2_orders",
            "v2_signal_receipts",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    binding = worker.entry_filter.binding_key(worker.scope.consumer, signal_id)
    immutable_binding = BusinessState(database).read(binding)

    def model(trend="0.8", chase="0.2"):
        def query(request, *, timeout_ms):
            assert 0 < timeout_ms <= 1500
            sdk_calls.append(request)
            return model_response(trend=trend, chase=chase)

        result = JevAdvisory(
            JevCoordinator(database, scope=PUBLIC_SCOPE, enabled=True),
            query,
            policy=provider_policy,
        ).evaluate(queued[0])
        assert result["status"] == "ADVISORY_READY", result
        # Reconstruct the gate: approval comes from PG, not process memory.
        worker.entry_filter = JevEntryContext(database, reference, scope=SCOPE)

    return SimpleNamespace(
        runtime=runtime,
        worker=worker,
        signal_id=signal_id,
        context=context,
        clock=clock,
        account=account,
        queued=queued,
        sdk_calls=sdk_calls,
        evaluations=evaluations,
        model=model,
        binding=binding,
        immutable_binding=immutable_binding,
    )


class SimulatedVenue(ReadinessVenue):
    """Protocol-faithful receipts; no live transport and no manual ledger fills."""

    def __init__(self, clock, plan):
        super().__init__()
        self.clock, self.posts, self.orders, self.fills = clock, [], {}, []
        self.parent, self.income = None, []
        self.rows["/fapi/v1/symbolConfig"][0].update(
            leverage=plan["leverage"], marginType=plan["margin_mode"]
        )
        self.balance(Decimal(1000))

    def balance(self, balance):
        self.rows["/fapi/v3/account"] = {
            name: str(balance)
            for name in ("totalWalletBalance", "availableBalance", "totalMarginBalance")
        }
        self.rows["/fapi/v3/account"]["assets"] = [
            {
                "asset": "USDT",
                "availableBalance": str(balance),
                "walletBalance": str(balance),
                "marginBalance": str(balance),
            }
        ]

    def fill(self, order_id, *, params, price, fee, when, pnl, trade_id):
        self.orders[order_id] = {
            "symbol": "BTCUSDT",
            "side": params["side"],
            "positionSide": "BOTH",
            "type": "MARKET",
            "reduceOnly": params["reduceOnly"] == "true",
            "orderId": order_id,
            "clientOrderId": params["newClientOrderId"],
            "origQty": params["quantity"],
            "executedQty": params["quantity"],
            "status": "FILLED",
            "avgPrice": str(price),
            "time": when,
        }
        self.fills.append(
            {
                "id": trade_id,
                "orderId": order_id,
                "symbol": "BTCUSDT",
                "side": params["side"],
                "positionSide": "BOTH",
                "qty": params["quantity"],
                "price": str(price),
                "commission": str(fee),
                "commissionAsset": "USDT",
                "time": when,
                "realizedPnl": str(pnl),
            }
        )

    def __call__(self, method, path, params):
        if method == "POST":
            self.posts.append((path, deepcopy(params)))
        if path == "/fapi/v1/order":
            if method == "POST":
                assert params["side"] == "BUY" and params["reduceOnly"] == "false"
                assert not self.orders, "duplicate opening POST"
                self.fill(
                    10,
                    params=params,
                    price=100,
                    fee=Decimal(params["quantity"]) * Decimal("0.04"),
                    when=self.clock.now(),
                    pnl=0,
                    trade_id=1,
                )
                self.rows["/fapi/v3/positionRisk"] = [
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "BOTH",
                        "positionAmt": params["quantity"],
                    }
                ]
                return deepcopy(self.orders[10])
            assert method == "GET"
            matches = [
                row
                for row in self.orders.values()
                if (
                    row["orderId"] == params.get("orderId")
                    or row["clientOrderId"] == params.get("origClientOrderId")
                )
            ]
            assert len(matches) == 1
            return deepcopy(matches[0])
        if path == "/fapi/v1/userTrades":
            assert method == "GET"
            return deepcopy(
                [row for row in self.fills if row["orderId"] == params["orderId"]]
            )
        if path == "/fapi/v1/algoOrder":
            if method == "POST":
                assert self.parent is None, "duplicate stop POST"
                self.parent = {
                    **params,
                    "orderType": params["type"],
                    "algoId": 30,
                    "algoStatus": "NEW",
                    "closePosition": True,
                    "priceProtect": False,
                }
                self.rows["/fapi/v1/openAlgoOrders"] = [deepcopy(self.parent)]
            else:
                assert method == "GET"
            return deepcopy(self.parent)
        if path == "/fapi/v1/income":
            assert method == "GET"
            return deepcopy(
                [
                    row
                    for row in self.income
                    if params["startTime"] <= row["time"] <= params["endTime"]
                ]
            )
        return super().__call__(method, path, params)

    def trigger_stop(self):
        opening = self.orders[10]
        quantity = Decimal(opening["origQty"])
        price = Decimal(self.parent["triggerPrice"])
        pnl, fee = (price - 100) * quantity, price * quantity * Decimal("0.0004")
        opened_at, closed_at = opening["time"], self.clock.now() + 2
        self.fill(
            555,
            params={
                "side": "SELL",
                "reduceOnly": "true",
                "newClientOrderId": "venue-native-stop",
                "quantity": opening["origQty"],
            },
            price=price,
            fee=fee,
            when=closed_at,
            pnl=pnl,
            trade_id=2,
        )
        self.parent.update(
            algoStatus="FINISHED", actualOrderId="555", actualQty=opening["origQty"]
        )
        self.rows["/fapi/v3/positionRisk"] = []
        self.rows["/fapi/v1/openAlgoOrders"] = []
        open_fee = Decimal(self.fills[0]["commission"])
        self.income = [
            {
                "incomeType": kind,
                "income": str(amount),
                "tradeId": trade,
                "tranId": index,
                "time": when,
                "symbol": "BTCUSDT",
                "asset": "USDT",
            }
            for index, (kind, amount, trade, when) in enumerate(
                [
                    ("COMMISSION", -open_fee, "1", opened_at),
                    ("COMMISSION", -fee, "2", closed_at),
                    ("REALIZED_PNL", pnl, "2", closed_at),
                    ("FUNDING_FEE", Decimal("-0.01"), "", opened_at + 1),
                ],
                1,
            )
        ]
        net = pnl - open_fee - fee - Decimal("0.01")
        self.balance(Decimal(1000) + net)
        return net


def test_jev_pending_to_original_risk_recheck_execution_stop_settlement_and_review(
    database,
    pipeline_case,
):
    case = prepare_candidate(database, pipeline_case)
    case.model()
    prepared = case.worker.consume(case.signal_id, context=case.context())
    assert prepared["status"] == "PREPARED", prepared
    assert len(case.evaluations) == 2 and case.evaluations[-1][1].action == "OPEN"
    assert (
        case.evaluations[0][0]["account_evidence"]
        != case.evaluations[1][0]["account_evidence"]
    )
    assert BusinessState(database).read(case.binding) == case.immutable_binding
    assert len(case.queued) == len(case.sdk_calls) == 1
    saved = case.worker.decision(case.signal_id)["snapshot"]
    evaluation = saved["features"]["evaluation"]
    assert evaluation["jev_entry_gate"]["status"] == "PASS"
    original = case.evaluations[-1][1]
    assert saved["result"]["quantity"] == original.quantity
    venue = SimulatedVenue(case.clock, evaluation["market_plan"])
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    runtime = case.runtime
    runtime.risk_check = lambda _: True
    runtime.execution.risk_reference = lambda order: RiskReference(
        order["environment"],
        order["symbol"],
        "test-mark",
        "100",
        case.clock.now(),
    )
    reference = lambda _: {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "mark_price": "100",
        "observed_at_ms": case.clock.now(),
        "tick_size": "0.01",
    }
    runtime.execution.submit = GuardedOpeningSubmit(
        database,
        readiness=TestnetVenueReadiness(venue, scope=SCOPE, clock_ms=case.clock.now),
        reference=reference,
        plan=lambda _: {
            "leverage": evaluation["market_plan"]["leverage"],
            "margin_type": evaluation["market_plan"]["margin_mode"],
        },
        submit=adapter.submit,
        enabled=True,
    )
    runtime.execution.query = adapter.query
    assert runtime.execution.dispatch(prepared["order_id"]) == "ACKNOWLEDGED"
    assert runtime.execution.dispatch(prepared["order_id"]) == "FILLED"
    assert runtime.execution.dispatch(prepared["order_id"]) == "FILLED"
    assert (
        case.worker.consume(case.signal_id, context={})["order_id"]
        == prepared["order_id"]
    )
    assert len(case.evaluations) == 2
    assert len(venue.posts) == 1
    assert runtime.account_risk.usage(SCOPE)["positions"] == 1

    def protection():
        return DirectionalProtectionStage(
            database,
            venue,
            scope=SCOPE,
            reference=reference,
            clock_ms=case.clock.now,
            allow_writes=True,
        )

    for _ in range(2):
        result = protection().run_once()
        assert result["status"] == "CLEAR", result
    assert len(venue.posts) == 2
    expected_net = venue.trigger_stop()
    case.clock.offset += 10
    for _ in range(2):
        result = protection().run_once()
        assert result["status"] == "CLEAR", result
    trace = runtime.data.trace(prepared["intent_id"])
    assert len(trace["orders"]) == len(trace["fills"]) == 2
    assert {order["leg"]: order["status"] for order in trace["orders"]} == {
        "OPEN": "FILLED",
        "CLOSE": "FILLED",
    }
    case.clock.offset += (
        60001  # Simulate bounded income-availability delay; never sleep.
    )
    settlement = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=case.clock.now
    )
    result = settlement.run_once()
    assert result["status"] == "CLEAR", result
    assert (
        Decimal(result["settlements"][prepared["intent_id"]]["net_pnl"]) == expected_net
    )
    trace = runtime.data.trace(prepared["intent_id"])
    assert trace["episode"]["status"] == "SETTLED"
    assert (
        len(trace["settlements"])
        == len(trace["cash"])
        == len(trace["income_allocations"])
        == 1
    )
    assert settlement.run_once()["status"] == "CLEAR"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone() == (1,)
        outcome = conn.execute(
            "SELECT net_pnl,evidence FROM v2_directional_outcomes WHERE episode_id=%s",
            (prepared["intent_id"],),
        ).fetchall()
    assert len(outcome) == 1 and outcome[0][0] == expected_net
    assert outcome[0][1]["source"] == "directional-outcome-v1"
    assert len(case.queued) == len(case.sdk_calls) == 1
    assert [path for path, _ in venue.posts] == ["/fapi/v1/order", "/fapi/v1/algoOrder"]


@pytest.mark.parametrize("reject_at", ["MODEL_VETO", "FRESH_ORIGINAL_RISK"])
def test_model_reference_never_overrides_admission_or_creates_rejected_intent(
    database,
    pipeline_case,
    reject_at,
):
    case = prepare_candidate(database, pipeline_case)
    case.model(trend="0.1" if reject_at == "MODEL_VETO" else "0.8")
    if reject_at == "FRESH_ORIGINAL_RISK":
        case.account["sizing"]["available_margin"] = "0"
    result = case.worker.consume(case.signal_id, context=case.context())
    assert result["status"] == "IGNORED", result
    assert len(case.evaluations) == 2
    if reject_at == "MODEL_VETO":
        assert case.evaluations[-1][1].action == "OPEN"
        assert result["reason"] == "JEV_TREND_UNSUPPORTED"
    else:
        assert case.evaluations[-1][1].action == "IGNORED"
        assert result["reason"] == case.evaluations[-1][1].rationale
    assert len(case.queued) == len(case.sdk_calls) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
