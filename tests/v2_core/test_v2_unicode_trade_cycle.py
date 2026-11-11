"""Exact Unicode identities and replay safety across the isolated PG trade cycle.

The shared database fixture rejects non-QA DSNs. Every venue operation below is
an in-memory fake; importing or collecting this module performs no I/O.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_v2_binance import order as protocol_order
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario, worker
from test_v2_directional_settlement import Venue as SettlementVenue
from test_v2_directional_settlement import baseline

from services.v2_directional_settlement import DirectionalSettlementStage
from tests.support.v2_admission import database as database_fixture
from v2_core.binance import BinanceFutures
from v2_core.ingress import SignalIngress
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState
from v2_core.symbols import shared_frame_id

database = database_fixture
SYMBOLS = ("牛来USDT", "\U00020000牛来USDT")


class UnicodeCycleVenue(SettlementVenue):
    """A strict, stateful fake for ordinary orders, protection and account cash."""

    def __init__(self, symbol):
        super().__init__(self.income, final_balance="989.894")
        self.symbol = symbol
        self.writes = []
        self.orders = {}
        self.trades = {}
        self.protection = None
        self.position = Decimal(0)

    def income(self, method, path, params):
        assert (method, path) == ("GET", "/fapi/v1/income")
        rows = (
            ("COMMISSION", "-0.05", "1", 1, 3),
            ("COMMISSION", "-0.046", "2", 2, 10),
            ("REALIZED_PNL", "-10", "2", 3, 10),
            ("FUNDING_FEE", "-0.01", "", 4, 5),
        )
        return [
            {
                "symbol": self.symbol,
                "asset": "USDT",
                "incomeType": kind,
                "income": amount,
                "tradeId": trade,
                "tranId": transaction,
                "time": occurred,
            }
            for kind, amount, trade, transaction, occurred in rows
        ]

    def __call__(self, method, path, params):
        if method != "GET":
            self.writes.append((method, path, deepcopy(params)))
        if path == "/fapi/v1/order":
            assert params["symbol"] == self.symbol
            if method == "POST":
                return self._submit(params)
            assert method == "GET"
            return deepcopy(self.orders[params["origClientOrderId"]])
        if path == "/fapi/v1/userTrades":
            assert method == "GET" and params["symbol"] == self.symbol
            return deepcopy(self.trades[params["orderId"]])
        if path == "/fapi/v1/algoOrder":
            return self._algo(method, params)
        if path == "/fapi/v3/positionRisk":
            assert method == "GET"
            return (
                []
                if not self.position
                else [
                    {
                        "symbol": self.symbol,
                        "positionSide": "BOTH",
                        "positionAmt": str(self.position),
                    }
                ]
            )
        if path == "/fapi/v1/openAlgoOrders":
            assert method == "GET"
            return (
                [deepcopy(self.protection)]
                if self.protection and self.protection["algoStatus"] == "NEW"
                else []
            )
        # The inherited QA inventory/income fake has no HTTP implementation.
        return super().__call__(method, path, params)

    def _submit(self, params):
        client = params["newClientOrderId"]
        assert client.isascii() and client not in self.orders
        assert params["quantity"] == "1.25"
        assert params["type"] == "MARKET" and params["positionSide"] == "BOTH"
        closing = params["reduceOnly"] == "true"
        assert params["side"] == ("SELL" if closing else "BUY")
        assert self.position == (Decimal("1.25") if closing else Decimal(0))
        order_id, trade_id = (11, 2) if closing else (10, 1)
        price, fee, occurred, pnl = (
            ("92", "0.046", 10, "-10") if closing else ("100", "0.05", 3, "0")
        )
        row = {
            "clientOrderId": client,
            "symbol": self.symbol,
            "side": params["side"],
            "positionSide": "BOTH",
            "type": "MARKET",
            "reduceOnly": closing,
            "origQty": "1.25",
            "executedQty": "1.25",
            "orderId": order_id,
            "status": "FILLED",
            "avgPrice": price,
            "time": occurred,
            "updateTime": occurred,
        }
        self.orders[client] = row
        self.trades[order_id] = [
            {
                "id": trade_id,
                "orderId": order_id,
                "symbol": self.symbol,
                "side": params["side"],
                "positionSide": "BOTH",
                "qty": "1.25",
                "price": price,
                "commission": fee,
                "commissionAsset": "USDT",
                "time": occurred,
                "realizedPnl": pnl,
            }
        ]
        self.position = Decimal(0) if closing else Decimal("1.25")
        return deepcopy(row)

    def _algo(self, method, params):
        if method == "POST":
            assert self.protection is None and params["symbol"] == self.symbol
            assert params["closePosition"] == "true"
            assert params["clientAlgoId"].isascii()
            self.protection = {
                **deepcopy(params),
                "orderType": params["type"],
                "closePosition": True,
                "priceProtect": False,
                "algoId": 42,
                "algoStatus": "NEW",
            }
        elif method == "DELETE":
            assert params == {"algoId": 42} and self.position == 0
            assert self.protection["algoStatus"] == "NEW"
            self.protection["algoStatus"] = "CANCELED"
        else:
            assert method == "GET"
            assert params == {"clientAlgoId": self.protection["clientAlgoId"]}
        return deepcopy(self.protection)


def adapter(venue):
    return BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )


def ingress(signals, *, now=3):
    return SignalIngress(
        signals,
        source="s3",
        environment=SCOPE.environment,
        clock_ms=lambda: now,
        max_age_ms=5,
        max_lifetime_ms=10,
    )


@pytest.mark.parametrize("target", SYMBOLS)
def test_unicode_cycle_fake_exercises_real_protocol_without_database(target):
    """Validate the PG test's venue fixtures separately without a database."""
    venue = UnicodeCycleVenue(target)
    protocol = adapter(venue)
    opening = {
        **protocol_order(),
        "account_id": SCOPE.account_id,
        "symbol": target,
        "quantity": "1.25",
    }
    for closing in (False, True):
        request = {
            **opening,
            "side": "SELL" if closing else "BUY",
            "reduce_only": closing,
            "client_order_id": "v2close" if closing else "v2open",
        }
        ack = protocol.submit(request)
        assert ack.status == "ACKNOWLEDGED"
        assert ack.evidence["submission_receipt"]["symbol"] == target
        observed = protocol.query(request)
        assert observed.status == "FILLED"
        assert observed.fills[0]["evidence"]["symbol"] == target
    assert venue.position == 0 and len(venue.writes) == 2


@pytest.mark.parametrize("target", SYMBOLS)
def test_unicode_signal_to_settlement_is_exact_and_replay_safe(database, target):
    _, runtime, _, context = scenario(database)
    original = runtime.data.signals.lookup(
        source="s3", environment=SCOPE.environment, request_key="replay-S6"
    )["snapshot"]
    event = {
        **original,
        "symbol": target,
        "event_id": shared_frame_id("s3-shared-v6", target, 1),
    }
    context["symbol"] = target
    signal = ingress(runtime.data.signals).accept(event)
    assert event["event_id"].isascii()
    assert ingress(runtime.data.signals).accept(deepcopy(event)) == signal
    admitted = formal(runtime).consume(signal, context=context)
    assert admitted["status"] == "PREPARED"
    episode, opening = admitted["intent_id"], admitted["order_id"]
    _, restarted = worker(database, now=4)
    replayed = formal(restarted).consume(signal, context={})
    assert (replayed["intent_id"], replayed["order_id"]) == (episode, opening)
    trace = restarted.data.trace(episode)
    assert trace["request"]["symbol"] == trace["decision"]["symbol"] == target
    assert trace["signal"]["snapshot"]["symbol"] == target
    assert trace["signal"]["request_key"] == event["event_id"]
    with database() as conn:
        slot = conn.execute(
            "SELECT slot_key FROM v2_episodes WHERE episode_id=%s", (episode,)
        ).fetchone()[0]
        assert json.loads(slot) == [*asdict(SCOPE).values(), target]
        assert conn.execute(
            "SELECT count(*) FROM v2_signal_receipts WHERE signal_id=%s", (signal,)
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)
    baseline(database, restarted, episode)
    venue = UnicodeCycleVenue(target)

    def runner():
        protocol = adapter(venue)
        return ExecutionRunner(
            database,
            submit=protocol.submit,
            query=protocol.query,
            risk_check=lambda _: True,
            scope=SCOPE,
        )

    assert runner().dispatch(opening) == "ACKNOWLEDGED"
    assert runner().recover(opening) == "FILLED"
    assert runner().dispatch(opening) == "FILLED"
    spec = ProtectionSpec(episode, target, "SELL", "STOP_MARKET", "92")

    def protection():
        return TestnetProtection(BusinessState(database), venue, scope=SCOPE)

    installed = protection().submit_once(spec)
    assert installed["status"] == "NEW"
    assert protection().submit_once(spec) == installed
    key = protection().key(spec)
    assert key.identity != protection().key(replace(spec, symbol="熊来USDT")).identity
    saved, payload = protection().read(spec)
    assert saved.version > 0 and payload["spec"]["symbol"] == target
    with database() as conn:
        scope, stored = conn.execute(
            "SELECT scope,payload FROM v2_business_state WHERE state_id=%s",
            (key.identity,),
        ).fetchone()
        assert scope == asdict(key) and stored == payload
    with pytest.raises(ValueError, match="cancel requires flat account"):
        protection().cancel_flat_once(spec)
    close_args = {
        "leg": "CLOSE",
        "quantity": "1.25",
        "request_key": "unicode-reduce-close",
        "evidence": {"source": "isolated-unicode-qa", "symbol": target},
    }
    closing, client = restarted.data.orders.prepare(episode, **close_args)
    assert restarted.data.orders.prepare(episode, **close_args) == (closing, client)
    reduction_checks = []

    def revalidate(request):
        assert request["symbol"] == target and request["reduce_only"] is True
        assert request["leg"] == "CLOSE" and request["side"] == "SELL"
        assert request["client_order_id"] == client and venue.position == Decimal(
            "1.25"
        )
        reduction_checks.append(request["order_id"])
        return True

    assert runner().dispatch(closing, reduce_only_check=revalidate) == "ACKNOWLEDGED"
    assert reduction_checks == [closing, closing]  # runner and adapter write boundary
    assert runner().recover(closing) == "FILLED"
    assert protection().cancel_flat_once(spec)["status"] == "CANCELED"
    assert protection().cancel_flat_once(spec)["status"] == "CANCELED"
    assert [write[:2] for write in venue.writes] == [
        ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/order"),
        ("DELETE", "/fapi/v1/algoOrder"),
    ]

    def settlement():
        return DirectionalSettlementStage(
            database, venue, scope=SCOPE, clock_ms=lambda: 70000
        )

    cash = settlement().cash.audit(episode)
    assert cash["status"] == "CASH_MATCHED", cash
    repeated_cash = settlement().cash.audit(episode)
    assert repeated_cash["status"] == "CASH_MATCHED", repeated_cash
    assert repeated_cash["income_digest"] == cash["income_digest"]
    result = settlement().run_once()
    assert result["status"] == "CLEAR", result
    assert result["settlements"][episode]["status"] == "SETTLED"
    settled = restarted.data.trace(episode)
    assert settled["episode"]["status"] == "SETTLED"
    assert len(settled["orders"]) == len(settled["fills"]) == 2
    assert len(settled["cash"]) == len(settled["settlements"]) == 1
    assert len(settled["income_allocations"]) == 1
    assert settled["cash"][0]["kind"] == "FUNDING"
    assert Decimal(settled["cash"][0]["amount"]) == Decimal("-0.01")
    receipts = [
        entry["evidence"]["submission_receipt"]
        for entry in settled["timeline"]
        if "submission_receipt" in entry["evidence"]
    ]
    assert len(receipts) == 2 and all(row["symbol"] == target for row in receipts)
    assert {row["reduce_only"] for row in receipts} == {False, True}
    proof = settled["settlements"][0]["evidence"]
    assert (
        Decimal(proof["wallet_delta"])
        == Decimal(proof["net_pnl"])
        == Decimal("-10.106")
    )
    assert proof["exchange_flat"] and proof["orders_terminal"]
    assert proof["fills_complete"] and proof["cash_complete"]
    for fill in settled["fills"]:
        assert json.loads(fill["fill_key"])[-2] == fill["evidence"]["symbol"] == target
    # Expired signal replay must preserve the old identity and deadline. A new
    # runtime, runner and protection journal must not depend on an in-memory latch.
    assert ingress(restarted.data.signals, now=70000).accept(event) == signal
    assert formal(restarted).consume(signal, context={})["order_id"] == opening
    for order_id in (opening, closing):
        assert runner().dispatch(order_id) == "FILLED"
        observed = adapter(venue).query(runner().snapshot(order_id))
        for fill in observed.fills:
            assert restarted.data.ledger.record_fill(order_id=order_id, **fill) is False
    assert settlement().run_once() == {
        "status": "CLEAR",
        "settlements": {},
        "outcomes": {},
        "settlement_authorized": False,
    }
    assert restarted.data.trace(episode) == settled
    assert len(venue.writes) == 4
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_exchange_income").fetchone() == (
            4,
        )
        assert conn.execute(
            "SELECT DISTINCT symbol FROM v2_exchange_income"
        ).fetchall() == [(target,)]
        assert conn.execute("SELECT count(*) FROM v2_fill_observations").fetchone() == (
            2,
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone() == (1,)
        assert conn.execute(
            "SELECT symbol FROM v2_directional_outcomes"
        ).fetchall() == [(target,)]
