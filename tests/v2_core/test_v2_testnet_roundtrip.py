import copy
import json
from decimal import Decimal

import pytest
from test_v2_intent_admission import database as database_fixture

from services import v2_testnet_roundtrip as rt
from v2_core.service import TradingData

database = database_fixture
INSTRUMENT = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "filters": [
        {
            "filterType": "MARKET_LOT_SIZE",
            "stepSize": "0.0001",
            "minQty": "0.0001",
            "maxQty": "1",
        },
        {
            "filterType": "PRICE_FILTER",
            "tickSize": "0.1",
            "minPrice": "100",
            "maxPrice": "1000000",
        },
        {"filterType": "MIN_NOTIONAL", "notional": "50"},
    ],
}


class Venue:
    account_id = rt.SCOPE.account_id
    environment = "SANDBOX"

    def __init__(self):
        self.orders, self.algos, self.writes = {}, {}, []
        self.quantity = "0"
        self.protection_fails = False
        self.lose_open = False

    def __call__(self, method, path, params):
        if method == "POST" and path.endswith("/order"):
            self.writes.append((path, dict(params)))
            closing = params["reduceOnly"] == "true"
            if closing:
                assert params["quantity"] == self.quantity
            self.quantity = "0" if closing else params["quantity"]
            raw = {
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": closing,
                "origQty": params["quantity"],
                "executedQty": params["quantity"],
                "orderId": len(self.orders) + 1,
                "clientOrderId": params["newClientOrderId"],
                "status": "FILLED",
            }
            self.orders[raw["clientOrderId"]] = raw
            if self.lose_open and not closing:
                raise TimeoutError("sensitive")
            return raw
        if method == "POST" and path.endswith("algoOrder"):
            self.writes.append((path, dict(params)))
            if self.protection_fails:
                raise TimeoutError("sensitive")
            assert Decimal(self.quantity) > 0
            raw = {
                **params,
                "orderType": params["type"],
                "closePosition": True,
                "priceProtect": False,
                "algoId": len(self.algos) + 1,
                "algoStatus": "NEW",
            }
            self.algos[params["clientAlgoId"]] = raw
            return raw
        if method == "DELETE":
            assert self.quantity == "0"
            row = next(
                a for a in self.algos.values() if a["algoId"] == params["algoId"]
            )
            row["algoStatus"] = "CANCELED"
            return row
        assert method == "GET"
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("multiAssetsMargin"):
            return {"multiAssetsMargin": False}
        if path.endswith("positionRisk"):
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": self.quantity,
                }
            ]
        if path.endswith("/account"):
            return {
                k: "1000"
                for k in (
                    "totalWalletBalance",
                    "availableBalance",
                    "totalMarginBalance",
                )
            }
        if path.endswith("openOrders"):
            return []
        if path.endswith("openAlgoOrders"):
            return [a for a in self.algos.values() if a["algoStatus"] == "NEW"]
        if path.endswith("algoOrder"):
            return copy.deepcopy(self.algos[params["clientAlgoId"]])
        if path.endswith("/order"):
            return self.orders[params["origClientOrderId"]]
        if path.endswith("userTrades"):
            o = next(
                o for o in self.orders.values() if o["orderId"] == params["orderId"]
            )
            return [
                {
                    "id": o["orderId"],
                    "orderId": o["orderId"],
                    "symbol": o["symbol"],
                    "side": o["side"],
                    "positionSide": "BOTH",
                    "qty": o["origQty"],
                    "price": "100000",
                    "commission": "0.01",
                    "commissionAsset": "USDT",
                    "time": rt.now(),
                    "realizedPnl": "0",
                }
            ]
        raise AssertionError(path)


def install(monkeypatch, venue):
    class Public:
        status = 200

        def __init__(self, host, timeout):
            assert host == "demo-fapi.binance.com" and timeout == 5

        def request(self, method, path):
            assert method == "GET"
            self.path = path

        def getresponse(self):
            return self

        def read(self, limit):
            data = (
                {"symbols": [INSTRUMENT]}
                if self.path.endswith("exchangeInfo")
                else {"symbol": "BTCUSDT", "markPrice": "100000", "time": rt.now()}
            )
            return json.dumps(data).encode()

        def close(self):
            pass

    def signed(**kwargs):
        assert kwargs["environment"] == "SANDBOX"
        assert kwargs["enable_trading"] and kwargs["enable_testnet_protection"]
        return venue

    monkeypatch.setattr(rt, "HTTPSConnection", Public)
    monkeypatch.setattr(rt, "BinanceSignedTransport", signed)
    monkeypatch.setattr(
        rt,
        "credentials",
        lambda *a, **kw: {
            "BINANCE_TESTNET_API_KEY": "dummy",
            "BINANCE_TESTNET_API_SECRET": "dummy",
        },
    )


@pytest.mark.parametrize("lost", [False, True])
def test_roundtrip_uses_real_pg_risk_orders_fills_and_clears_account(
    database, monkeypatch, lost
):
    venue = Venue()
    venue.lose_open = lost
    install(monkeypatch, venue)
    rt.execute(database, "unused")
    trace = TradingData(database).trace(rt.EPISODE)
    assert len(trace["orders"]) == 2
    assert all(o["status"] == "FILLED" for o in trace["orders"])
    assert len(trace["fills"]) == 2
    assert trace["account_risk"]["notional"] == "60.000000000000000000"
    assert venue.quantity == "0"
    assert all(a["algoStatus"] == "CANCELED" for a in venue.algos.values())
    assert len(venue.writes) == 4
    rt.execute(database, "unused")
    assert len(venue.writes) == 4


def test_failed_protection_still_closes_the_owned_position(database, monkeypatch):
    venue = Venue()
    venue.protection_fails = True
    install(monkeypatch, venue)
    with pytest.raises(KeyError):
        rt.execute(database, "unused")
    assert venue.quantity == "0"
    orders = TradingData(database).trace(rt.EPISODE)["orders"]
    assert len(orders) == 2 and all(o["status"] == "FILLED" for o in orders)
    assert venue.writes[-1][1]["reduceOnly"] == "true"


def test_preexisting_exposure_blocks_before_open(database, monkeypatch):
    venue = Venue()
    venue.quantity = "0.1"
    install(monkeypatch, venue)
    with pytest.raises(ValueError, match="CLEAR_ACCOUNT"):
        rt.execute(database, "unused")
    assert venue.writes == []


@pytest.mark.parametrize("trigger_kind", ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
def test_native_stop_closes_without_a_second_post_and_survives_restart(
    database, monkeypatch, trigger_kind
):
    class TriggerVenue(Venue):
        queries = 0

        def __call__(self, method, path, params):
            if method == "GET" and path.endswith("algoOrder"):
                self.queries += 1
                row = self.algos[params["clientAlgoId"]]
                if self.queries == 2:
                    quantity = self.quantity
                    self.quantity = "0"
                    self.orders["venue-child"] = {
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "positionSide": "BOTH",
                        "type": "MARKET",
                        "reduceOnly": True,
                        "origQty": quantity,
                        "executedQty": quantity,
                        "orderId": 2,
                        "clientOrderId": "venue-child",
                        "status": "FILLED",
                    }
                    row.update(
                        algoStatus="FINISHED", actualOrderId="2", actualQty=quantity
                    )
                return copy.deepcopy(row)
            if method == "GET" and path.endswith("/order") and "orderId" in params:
                return copy.deepcopy(
                    next(
                        o
                        for o in self.orders.values()
                        if o["orderId"] == params["orderId"]
                    )
                )
            return super().__call__(method, path, params)

    campaign, episode = rt.trigger_identity(1)
    monkeypatch.setattr(rt, "CAMPAIGN", campaign)
    monkeypatch.setattr(rt, "EPISODE", episode)
    venue = TriggerVenue()
    install(monkeypatch, venue)
    rt.execute(database, "unused", trigger=True, trigger_kind=trigger_kind)
    trace = TradingData(database).trace(episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    assert len([w for w in venue.writes if w[0].endswith("/order")]) == 1
    assert (
        next(o for o in trace["orders"] if o["leg"] == "CLOSE")["request_evidence"][
            "origin"
        ]
        == "BINANCE_ALGO_CHILD"
    )
    count = len(venue.writes)
    rt.execute(database, "unused", trigger=True, trigger_kind=trigger_kind)
    assert len(venue.writes) == count


@pytest.mark.parametrize(
    "price,age", [("100000", 10001), ("100000", -1), ("NaN", 0), ("100", 0)]
)
def test_plan_rejects_stale_future_or_invalid_market(price, age):
    with pytest.raises(ValueError):
        rt.plan_from_market(
            INSTRUMENT,
            {"symbol": "BTCUSDT", "markPrice": price, "time": 20000 - age},
            20000,
        )
