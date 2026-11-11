"""Offline protocol integration, NOT proof of a real Testnet venue fill.

No direct fill/order-status insertion: real adapters reconcile simulated venue
responses through PG risk reservation, entry gate, stop, exit and settlement.
"""

import json
import time
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal

import pytest
from test_v2_directional import history, inputs, sizing
from test_v2_directional_exit_stage import observation
from test_v2_directional_replay import SCOPE
from test_v2_quant_indicators import minutes
from test_v2_quant_research import Archive, snapshot
from test_v2_venue_readiness import Venue as ReadyVenue

from services.v2_directional_admission import create_directional_admission_worker
from services.v2_directional_execution import bind_directional_venue_gate
from services.v2_directional_exit import DirectionalExitStage
from services.v2_directional_lifecycle import DirectionalProtectionStage
from services.v2_directional_settlement import DirectionalSettlementStage
from services.v2_quant_research import QuantEvidenceContext, QuantPolicy, QuantResearch
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountPolicy, RiskReference
from v2_core.binance import BinanceFutures
from v2_core.evidence import canonical
from v2_core.runtime import DataRuntime
from v2_core.runtime_policy import PolicyStore

database = database_fixture


class Venue(ReadyVenue):
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, clock, price, *, lost=False):
        super().__init__()
        self.clock, self.price, self.lost = clock, Decimal(price), lost
        self.wallet, self.position = Decimal(1000), Decimal(0)
        self.delete_calls = 0
        self.orders, self.trades, self.algos, self.income, self.posts = (
            {},
            [],
            {},
            [],
            [],
        )

    def __call__(self, method, path, params):
        if method == "POST":
            self.posts.append((path, deepcopy(params)))
        if path == "/fapi/v3/account":
            return {
                "totalWalletBalance": str(self.wallet),
                "availableBalance": str(self.wallet),
                "totalMarginBalance": str(self.wallet),
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": str(self.wallet),
                        "availableBalance": str(self.wallet),
                        "marginBalance": str(self.wallet),
                    }
                ],
            }
        if path == "/fapi/v3/positionRisk":
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": str(self.position),
                }
            ]
        if path == "/fapi/v1/openAlgoOrders":
            return [
                deepcopy(a) for a in self.algos.values() if a["algoStatus"] == "NEW"
            ]
        if path == "/fapi/v1/order":
            if method == "GET":
                return deepcopy(self.orders[params["origClientOrderId"]])
            assert method == "POST"
            quantity = Decimal(params["quantity"])
            closing = params["reduceOnly"] == "true"
            direction = 1 if params["side"] == "BUY" else -1
            if closing:
                assert quantity == abs(self.position) and direction * self.position < 0
            else:
                assert self.position == 0
                self.entry = self.price
            pnl = (self.price - self.entry) * self.position if closing else Decimal(0)
            fee = quantity * self.price * Decimal(".0004")
            self.position += direction * quantity
            self.wallet += pnl - fee
            identity = len(self.orders) + 1
            raw = {
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": closing,
                "origQty": str(quantity),
                "executedQty": str(quantity),
                "orderId": identity,
                "clientOrderId": params["newClientOrderId"],
                "status": "FILLED",
            }
            self.orders[raw["clientOrderId"]] = raw
            self.trades.append(
                {
                    "id": identity,
                    "orderId": identity,
                    "symbol": "BTCUSDT",
                    "side": params["side"],
                    "positionSide": "BOTH",
                    "qty": str(quantity),
                    "price": str(self.price),
                    "commission": str(fee),
                    "commissionAsset": "USDT",
                    "time": self.clock(),
                    "realizedPnl": str(pnl),
                }
            )
            for kind, amount in (("COMMISSION", -fee), ("REALIZED_PNL", pnl)):
                if amount:
                    self.income.append(
                        {
                            "incomeType": kind,
                            "income": str(amount),
                            "tradeId": str(identity),
                            "tranId": len(self.income) + 1,
                            "time": self.clock(),
                            "symbol": "BTCUSDT",
                            "asset": "USDT",
                        }
                    )
            if self.lost and not closing:
                raise TimeoutError("simulated lost response after accepted POST")
            return deepcopy(raw)
        if path == "/fapi/v1/userTrades":
            return [
                deepcopy(t)
                for t in self.trades
                if t["orderId"] == params["orderId"] and t["id"] >= params["fromId"]
            ]
        if path == "/fapi/v1/algoOrder":
            if method == "POST":
                assert self.position != 0
                self.algos[params["clientAlgoId"]] = {
                    **params,
                    "orderType": params["type"],
                    "algoId": 100,
                    "algoStatus": "NEW",
                    "closePosition": True,
                    "priceProtect": False,
                }
            elif method == "DELETE":
                self.delete_calls += 1
                assert self.position == 0
                next(a for a in self.algos.values() if a["algoId"] == params["algoId"])[
                    "algoStatus"
                ] = "CANCELED"
                if self.lost:
                    raise TimeoutError("simulated accepted DELETE with lost response")
                return {}
            return deepcopy(self.algos[params["clientAlgoId"]])
        if path == "/fapi/v1/income":
            return [
                deepcopy(r)
                for r in self.income
                if params["startTime"] <= r["time"] <= params["endTime"]
            ]
        return super().__call__(method, path, params)


def setup(database, *, strategy="S6", lost=False, admit=True):
    clock = [time.time_ns() // 1000000]
    closed = clock[0] // 60000 * 60000
    rows = minutes()
    for row in rows:
        row["t"] += closed - 1440 * 60000
    price = "102" if strategy == "S6" else "98"
    rows[-1].update(
        o=price,
        c=price,
        h=str(Decimal(price) + Decimal(".1")),
        l=str(Decimal(price) - Decimal(".1")),
        v="30",
    )
    archive = Archive(rows)
    policy = QuantPolicy(mode="CANDIDATE", timeframes=(1,))
    policy.save(database, "SANDBOX", expected_version=0, request_key="quant-QA")
    research = QuantResearch(
        database, archive=archive, environment="SANDBOX", clock_ms=lambda: clock[0]
    )
    signal_id = research.observe("BTCUSDT", list(reversed(rows)), closed)[
        "observations"
    ][0]["signal_id"]
    assert signal_id
    venue = Venue(lambda: clock[0], price, lost=lost)
    venue.archive = archive
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runtime = DataRuntime(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: True,
        clock_ms=lambda: clock[0],
        scope=SCOPE,
        risk_reference=lambda _: RiskReference(
            "SANDBOX", "BTCUSDT", "quant-QA", price, time.time_ns() // 1000000
        ),
    )
    runtime.account_risk.configure(
        SCOPE,
        AccountPolicy("USDT", "1000", 10, 0, "quant-QA", 60000),
        expected_version=0,
    )
    settings = (
        PolicyStore(database, SCOPE)
        .patch(
            {
                "quant.admission_enabled": True,
                "sizing.risk_fraction": ".005",
                "sizing.max_margin_fraction": ".05",
                "sizing.pool_fraction": ".70",
            },
            expected_version=0,
            reason="isolated QA",
        )
        .values
    )
    worker = create_directional_admission_worker(
        runtime,
        strategy=strategy,
        analysis_mode="hard",
        max_delay_ms=30000,
        enable_admission=True,
        source="quant",
        dynamic_universe=True,
    )
    worker.config_json = canonical(
        {**json.loads(worker.config_json), "policy": settings}
    )
    limits = sizing()
    for name in ("price", "atr_pct", "analysis_factor"):
        limits.pop(name)
    signal = snapshot(database, signal_id)
    context = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "account_scope": asdict(SCOPE),
        "assembled_at": clock[0],
        "valid_until_ms": clock[0] + 30000,
        "sources": {"archive": {"id": "isolated-QA"}},
        "signal_fusion": {"window_ms": 60000, "peers": []},
        "universe_admission": {
            "eligible": True,
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
        },
        "directional": {
            "market": inputs(strategy)["market"],
            "price": price,
            "regime": "neutral",
            "short_ratio": None,
            "history": history(),
            "sizing": limits,
            "expected_move_pct": signal["features"]["quant_target_move_pct"],
            "funding_rate": "0",
        },
    }
    verified = QuantEvidenceContext(database, lambda _: context, archive=archive)(
        signal
    )
    if not admit:
        return runtime, signal_id, venue, clock, worker, verified
    result = worker.consume(signal_id, context=verified)
    assert result["status"] == "PREPARED", result
    mark = lambda _: {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "mark_price": str(venue.price),
        "tick_size": ".01",
        "observed_at_ms": clock[0],
    }
    gate = bind_directional_venue_gate(
        runtime, venue, mark_reference=mark, enabled=True, reduce_only_enabled=True
    )
    return runtime, result, venue, clock, mark, gate


@pytest.mark.parametrize("strategy", ["S6", "S8"])
@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize("exit_mode", ["funding", "atr"])
def test_minutes_to_settled_strategy_outcome_without_manual_ledger_writes(
    database, strategy, lost, exit_mode
):
    runtime, result, venue, clock, mark, _gate = setup(
        database, strategy=strategy, lost=lost
    )
    order, episode = result["order_id"], result["intent_id"]
    assert runtime.execution.dispatch(order) == ("UNKNOWN" if lost else "ACKNOWLEDGED")
    assert runtime.execution.recover(order) == "FILLED"
    assert runtime.execution.dispatch(order) == "FILLED"
    protection = DirectionalProtectionStage(
        database,
        venue,
        scope=SCOPE,
        reference=mark,
        clock_ms=lambda: clock[0],
        allow_writes=True,
    )
    protected = protection.run_once()
    assert protected["status"] == "CLEAR", protected
    assert protection.run_once()["status"] == "CLEAR"
    assert len(venue.orders) == len(venue.algos) == 1
    from test_v2_account_inventory import TelegramConnection, sink

    from v2_core.trade_notifications import TradeLifecycleNotifications

    telegram = TelegramConnection()
    notifications = TradeLifecycleNotifications(database, sink(telegram), scope=SCOPE)
    assert notifications.run_once()["delivered"] == 1
    opening_message = json.loads(telegram.calls[0][1]["body"])["text"]
    assert "开仓成功" in opening_message and "QUANT_DONCHIAN_" in opening_message
    assert "UTC+8" in opening_message and "杠杆：2x ISOLATED" in opening_message
    clock[0] += 1000
    venue.price += Decimal(".2") if strategy == "S6" else Decimal("-.2")
    exits = DirectionalExitStage(
        database,
        venue,
        runtime=runtime,
        scope=SCOPE,
        observe=lambda _: observation(
            clock[0],
            mark_price=str(venue.price),
            funding_rate=".006" if strategy == "S6" else "-.006",
        ),
        clock_ms=lambda: clock[0],
        reference=mark,
        allow_writes=True,
    )
    if exit_mode == "atr":
        from services.v2_quant_exit import QuantExitMarket

        PolicyStore(database, SCOPE).patch(
            {"quant.trailing_exit_enabled": True},
            expected_version=1,
            reason="QA trailing",
        )
        # Keep the same archived candle without moving the simulated close
        # back onto the opening fill timestamp. The cash proof deliberately
        # rejects an ambiguous opening/closing order within one millisecond.
        clock[0] -= 999
        exits.quant_market = QuantExitMarket(venue.archive)
        exits.observe = lambda _: observation(
            clock[0], mark_price=str(venue.price), funding_rate="0"
        )
        venue.price = venue.entry + (Decimal(2) if strategy == "S6" else Decimal(-2))
        assert exits.evaluate(episode)["status"] == "WAIT"
        venue.price = venue.entry + (
            Decimal(".2") if strategy == "S6" else Decimal("-.2")
        )
    answer = exits.evaluate(episode)
    assert answer["status"] == "DISPATCHED" and answer["reason"] == (
        "ADVERSE_FUNDING" if exit_mode == "funding" else "QUANT_ATR_TRAIL"
    ), answer
    assert venue.position == 0
    assert runtime.execution.recover(answer["order_id"]) == "FILLED"
    assert len(venue.orders) == 2
    assert all(
        a["algoStatus"] == "NEW" for a in venue.algos.values()
    )  # hard stop remained throughout exit
    cleanup = protection.run_once()
    if lost:
        assert cleanup["status"] == "BLOCKED", cleanup
        cleanup = protection.run_once()
    assert cleanup["status"] == "CLEAR", cleanup
    assert venue.delete_calls == 1
    clock[0] += 70000
    settle = DirectionalSettlementStage(
        database, venue, scope=SCOPE, clock_ms=lambda: clock[0]
    )
    settled = settle.settle(episode)
    assert settled["status"] == "SETTLED", settled
    trace = runtime.data.trace(episode)
    assert len(trace["fills"]) == 2 and len(trace["settlements"]) == 1
    assert (
        Decimal(trace["settlements"][0]["evidence"]["net_pnl"]) == venue.wallet - 1000
    )
    assert (
        trace["decision"]["features"]["evaluation"]["source_scoring"]["strategy_id"]
        == "donchian"
    )
    with database() as conn:
        assert conn.execute(
            "SELECT event_type FROM v2_directional_outcomes WHERE episode_id=%s",
            (episode,),
        ).fetchone() == ("QUANT_DONCHIAN_" + ("UP" if strategy == "S6" else "DOWN"),)
    assert settle.run_once()["status"] == "CLEAR"
    assert len(venue.orders) == 2
    assert notifications.run_once()["delivered"] == 1
    closing_message = json.loads(telegram.calls[1][1]["body"])["text"]
    assert "平仓" in closing_message and "净收益" in closing_message
    assert notifications.run_once()["claimed"] == 0 and len(telegram.calls) == 2
    from services.v2_dashboard import DashboardData
    from services.v2_performance_reporting import signal_funnel

    with database() as conn:
        assert signal_funnel(conn, SCOPE.account_id) == [("quant", 1, 1, 1, 1)]
        assert signal_funnel(conn, "different-account") == [("quant", 1, 0, 0, 0)]
    dashboard = DashboardData(database).overview()
    assert dashboard["summary"]["settled_trades"] == 1
    assert dashboard["signals"][0]["source"] == "quant"
    assert dashboard["trades"][0]["signal_source"] == "quant"
    from services.v2_strategy_analytics import account_analytics, sizing_impact

    with database() as conn:
        report = account_analytics(conn, SCOPE, now_ms=clock[0])
        impact = sizing_impact(conn, SCOPE, {"sizing.max_margin_fraction": "0.10"})
    assert report["summary"]["settled_trades"] == 1
    assert Decimal(report["summary"]["net_pnl"]) == venue.wallet - 1000
    assert (
        report["trades"][0]["sizing_explanation"]["version"] == "directional-sizing-v1"
    )
    assert report["trades"][0]["excursions"]["samples"] >= 1
    assert report["groups"][0]["event_type"].startswith("QUANT_DONCHIAN_")
    assert report["groups"][0]["timeframe_minutes"] == 1
    assert impact["replayed"] == 1 and impact["pnl_comparison"] is None
    from v2_core.directional_outcomes import DirectionalHistory

    signal = snapshot(database, trace["decision"]["signal_id"])
    historical = DirectionalHistory(
        database, scope=SCOPE, producer=strategy.lower(), clock_ms=lambda: clock[0]
    )
    assert historical(signal)["stats"]["trades"] == 1
    changed = deepcopy(signal)
    changed["features"]["quant_timeframe_minutes"] = 15
    assert historical(changed)["stats"]["trades"] == 0
    changed = deepcopy(signal)
    changed["features"]["quant_config_digest"] = "0" * 64
    assert historical(changed)["stats"]["trades"] == 0


@pytest.mark.parametrize("disable", ["account", "global", "plugin", "timeframe"])
def test_prepared_quant_cannot_open_after_policy_is_disabled(database, disable):
    runtime, result, venue, _clock, _mark, _gate = setup(database)
    if disable == "account":
        PolicyStore(database, SCOPE).patch(
            {"quant.admission_enabled": False}, expected_version=1, reason="QA stop"
        )
    elif disable == "global":
        QuantPolicy().save(database, "SANDBOX", expected_version=1, request_key="stop")
    else:
        QuantPolicy(
            mode="CANDIDATE",
            timeframes=(15,) if disable == "timeframe" else (1,),
            plugins=("squeeze",) if disable == "plugin" else ("donchian", "squeeze"),
        ).save(database, "SANDBOX", expected_version=1, request_key="stop")
    assert runtime.execution.dispatch(result["order_id"]) == "REJECTED"
    assert venue.orders == {} and venue.position == 0


def test_prepared_quant_cannot_chase_after_actual_mark_moves(database):
    runtime, result, venue, _clock, _mark, _gate = setup(database)
    venue.price = Decimal(110)
    assert runtime.execution.dispatch(result["order_id"]) == "REJECTED"
    assert venue.orders == {} and venue.position == 0


@pytest.mark.parametrize("other_source", ["tv_bridge", "quant"])
def test_concurrent_tv_or_quant_candidate_shares_one_opportunity(
    database, other_source
):
    from concurrent.futures import ThreadPoolExecutor

    runtime, signal_id, venue, _clock, worker, context = setup(database, admit=False)
    other_snapshot = snapshot(database, signal_id)
    if other_source == "tv_bridge":
        other_snapshot["signal"] = "TREND_UP"
        other_snapshot["features"] = {"strength": 90, "price": str(venue.price)}
    other_id = runtime.data.signals.admit(
        source=other_source,
        environment="SANDBOX",
        request_key="qa-correlated",
        snapshot=other_snapshot,
    )
    second = create_directional_admission_worker(
        runtime,
        strategy="S6",
        analysis_mode="hard",
        max_delay_ms=30000,
        enable_admission=True,
        source=other_source,
        dynamic_universe=True,
        tv_start_ms=1,
    )
    second.config_json = canonical(
        {
            **json.loads(second.config_json),
            "policy": json.loads(worker.config_json)["policy"],
        }
    )
    other_context = deepcopy(context)
    if other_source == "tv_bridge":
        # Use the established TV fixture's movement assumption. The quant
        # ATR target is not a forecast for the legacy strategy's wider stop.
        other_context["directional"]["expected_move_pct"] = "8"
    for candidate, identifier, evidence in (
        (worker, signal_id, context),
        (second, other_id, other_context),
    ):
        decision = candidate.decide(
            snapshot(database, identifier), evidence, json.loads(candidate.config_json)
        )
        assert decision.action == "OPEN", decision
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(w.consume, identifier, context=deepcopy(evidence))
            for w, identifier, evidence in (
                (worker, signal_id, context),
                (second, other_id, other_context),
            )
        ]
        results = [f.result() for f in futures]
    assert sorted(r["status"] for r in results) == ["IGNORED", "PREPARED"], results
    assert (
        next(r for r in results if r["status"] == "IGNORED")["reason"]
        == "CORRELATED_SIGNAL_ALREADY_ADMITTED"
    )
    assert not venue.posts and not venue.orders
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 1
