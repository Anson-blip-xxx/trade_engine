import json
from copy import deepcopy

import pytest
from market_fakes import Budget, MarketHTTP

from services.v2_directional_exit_market import BinanceDirectionalExitMarket
from v2_core.public_market import BinancePublicMarket, PublicMarketError


class Market:
    environment = "SANDBOX"

    def __init__(self, *, server=86400000):
        self.server, self.calls = server, []
        self.mutate = lambda path, value: value

    def __call__(self, path, params):
        self.calls.append((path, deepcopy(params)))
        if path == "/fapi/v1/time":
            value = {"serverTime": self.server}
        elif path == "/fapi/v1/premiumIndex":
            value = {
                "symbol": "BTCUSDT",
                "markPrice": "100.25",
                "lastFundingRate": "-.0001",
                "time": self.server,
            }
        else:
            width = 900000 if params["interval"] == "15m" else 3600000
            count = params["limit"]
            start = params["startTime"]
            closes = (
                ["97", "98", "99", "100"]
                if params["interval"] == "15m"
                else [str(value) for value in range(81, 101)]
            )
            assert count == len(closes)
            value = [
                [
                    start + index * width,
                    close,
                    close,
                    close,
                    close,
                    "1",
                    start + (index + 1) * width - 1,
                    "1",
                    1,
                    "1",
                    "1",
                    "0",
                ]
                for index, close in enumerate(closes)
            ]
        return self.mutate(path, value)


def provider(market=None, *, clock=86400000, **options):
    return BinanceDirectionalExitMarket(
        market or Market(),
        environment="SANDBOX",
        clock_ms=lambda: clock,
        exit_fee_rate=".0004",
        **options,
    )


def test_fresh_mark_funding_and_closed_windows_are_exact():
    market = Market()
    result = provider(market)("BTCUSDT")
    assert result == {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "observed_at_ms": 86400000,
        "mark_price": "100.25",
        "funding_rate": "-0.0001",
        "ema9_1h": "96",
        "ema20_1h": "90.5",
        "momentum_closes_15m": ["97", "98", "99", "100"],
        "exit_fee_rate": "0.0004",
        "source": "binance-public-exit-v1",
        "exchange_server_time_ms": 86400000,
        "closed_15m_at_ms": 86400000,
        "closed_1h_at_ms": 86400000,
    }
    assert market.calls == [
        ("/fapi/v1/time", {}),
        ("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}),
        (
            "/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": 82800000,
                "endTime": 86399999,
                "limit": 4,
            },
        ),
        (
            "/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "1h",
                "startTime": 14400000,
                "endTime": 86399999,
                "limit": 20,
            },
        ),
    ]


def test_recurring_ema_is_bounded_to_the_ledger_numeric_scale():
    market = Market()

    def recurring(path, value):
        if path.endswith("klines") and len(value) == 20:
            for row in value:
                row[4] = "1"
            value[-1][4] = "2"
        return value

    market.mutate = recurring
    result = provider(market)("BTCUSDT")
    assert result["ema9_1h"] == "1.111111111111111111"
    assert result["ema20_1h"] == "1.05"


@pytest.mark.parametrize(
    "defect",
    ["clock", "premium", "premium_time", "missing", "order", "decimal"],
)
def test_incomplete_stale_or_malformed_observation_fails_closed(defect):
    market = Market()
    if defect == "clock":
        market.server -= 6000
    elif defect == "premium":
        market.mutate = lambda path, value: (
            {**value, "symbol": "ETHUSDT"} if path == "/fapi/v1/premiumIndex" else value
        )
    elif defect == "premium_time":
        market.mutate = lambda path, value: (
            {**value, "time": value["time"] - 6000}
            if path == "/fapi/v1/premiumIndex"
            else value
        )
    elif defect == "missing":
        market.mutate = lambda path, value: (
            value[:-1] if path.endswith("klines") else value
        )
    elif defect == "order":
        market.mutate = lambda path, value: (
            list(reversed(value)) if path.endswith("klines") else value
        )
    else:
        market.mutate = lambda path, value: (
            [[*value[0][:4], 100, *value[0][5:]], *value[1:]]
            if path.endswith("klines")
            else value
        )
    with pytest.raises(ValueError):
        provider(market)("BTCUSDT")


def test_public_transport_only_accepts_exact_exit_query_shapes():
    premium = json.dumps(
        {
            "symbol": "BTCUSDT",
            "markPrice": "100",
            "lastFundingRate": "0",
            "time": 1,
        }
    ).encode()
    budget, http = Budget(), MarketHTTP(raw=premium)
    client = BinancePublicMarket(
        environment="SANDBOX",
        budget=budget,
        enabled=True,
        connection_factory=http,
    )
    assert client("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})["markPrice"] == "100"
    assert budget.weights == [1]
    with pytest.raises((PublicMarketError, ValueError)):
        client("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT", "apiKey": "x"})
    with pytest.raises(ValueError):
        client(
            "/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": 0,
                "endTime": 1,
                "limit": 4,
            },
        )
    assert len(http.calls) == 1


@pytest.mark.parametrize("environment", ["LIVE", "", None])
def test_provider_is_testnet_only(environment):
    with pytest.raises(ValueError):
        BinanceDirectionalExitMarket(
            Market(),
            environment=environment,
            clock_ms=lambda: 1,
            exit_fee_rate=".0004",
        )
