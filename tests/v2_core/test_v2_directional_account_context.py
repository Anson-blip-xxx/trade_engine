import time
from copy import deepcopy

import pytest
from test_v2_directional import history as historical_stats
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from services.v2_directional_account_context import (
    BinanceDirectionalAccountContext,
    expected_move,
)
from v2_core.drawdown import DrawdownState

database = database_fixture


class Clock:
    def __init__(self):
        self.now = time.time_ns() // 1_000_000

    def __call__(self):
        self.now += 1
        return self.now


class Request:
    account_id = SCOPE.account_id
    environment = "SANDBOX"

    def __init__(self):
        self.calls = []
        self.positions = [
            [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}],
            [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}],
        ]
        self.account = {
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "700",
            "totalInitialMargin": "100",
            "totalOpenOrderInitialMargin": "20",
        }
        self.config = [{"symbol": "BTCUSDT", "maxNotionalValue": "50000"}]

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        assert method == "GET"
        if path == "/fapi/v3/positionRisk":
            return deepcopy(self.positions.pop(0))
        if path == "/fapi/v3/account":
            return deepcopy(self.account)
        if path == "/fapi/v1/symbolConfig":
            assert params == {"symbol": "BTCUSDT"}
            return deepcopy(self.config)
        raise AssertionError("unexpected signed route")


class Public:
    environment = "SANDBOX"

    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.exchange = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "100",
                            "stepSize": "0.001",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }
        self.ratio_time = None
        self.funding_time = None

    def __call__(self, path, params):
        self.calls.append((path, deepcopy(params)))
        if path == "/fapi/v1/exchangeInfo":
            return deepcopy(self.exchange)
        if path == "/futures/data/globalLongShortAccountRatio":
            return [
                {
                    "symbol": "BTCUSDT",
                    "shortAccount": "0.47",
                    "timestamp": self.ratio_time or self.clock.now,
                }
            ]
        if path == "/fapi/v1/premiumIndex":
            return {
                "symbol": "BTCUSDT",
                "lastFundingRate": "-0.0001",
                "time": self.funding_time or self.clock.now,
            }
        raise AssertionError("unexpected public route")


class LiveSentiment:
    environment = "LIVE"

    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def __call__(self, path, params):
        self.calls.append((path, deepcopy(params)))
        assert path == "/futures/data/globalLongShortAccountRatio"
        return [
            {
                "symbol": "BTCUSDT",
                "shortAccount": "0.47",
                "timestamp": self.clock.now - 3600000,
            }
        ]


def signal(kind="TREND_UP", **features):
    values = {
        "TREND_UP": {"chg_1h": "4"},
        "PULSE_UP": {"chg_15m": "-5.25"},
        "VIOLENT_BULLISH": {"vol_1h": "18"},
    }
    return {
        "symbol": "BTCUSDT",
        "signal": kind,
        "features": {**values.get(kind, {}), **features},
    }


def build(database, *, sentiment=None):
    clock, request = Clock(), Request()
    public = Public(clock)
    stats = historical_stats()

    def history(_):
        return {
            "stats": deepcopy(stats),
            "evidence": {"source": "pg-directional-outcomes-v1"},
            "observed_at_ms": clock.now - 100,
            "valid_until_ms": clock.now + 60000,
        }

    drawdown = DrawdownState(
        database,
        SCOPE,
        source="binance-futures-account-v3",
        currency="USDT",
        max_age_ms=60000,
    )
    provider = BinanceDirectionalAccountContext(
        database,
        request,
        public,
        history,
        drawdown,
        scope=SCOPE,
        clock_ms=clock,
        sentiment_market=sentiment,
    )
    return provider, request, public, clock, stats


def test_real_sources_are_normalized_persisted_and_bound_to_drawdown(database):
    provider, request, public, _, stats = build(database)
    result = provider(signal())
    assert result["account_scope"]["account_id"] == SCOPE.account_id
    assert result["history"] == stats
    assert result["short_ratio"] == "0.47"
    assert result["funding_rate"] == "-0.0001"
    assert result["expected_move_pct"] == "4"
    assert result["sizing"] == {
        "balance": "1000",
        "available_margin": "700",
        "used_pool_margin": "120",
        "quantity_step": "0.001",
        "price_tick": "0.1",
        "min_quantity": "0.001",
        "max_quantity": "100",
        "min_notional": "5",
        "max_notional": "50000",
        "drawdown_factor": "1",
    }
    assert all(method == "GET" for method, *_ in request.calls)
    assert [path for path, _ in public.calls] == [
        "/fapi/v1/exchangeInfo",
        "/futures/data/globalLongShortAccountRatio",
        "/fapi/v1/premiumIndex",
    ]
    with database() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (result["evidence"]["state_id"],),
        ).fetchone()[0]
    assert payload["balance"] == "1000" and payload["rules"]["price_tick"] == "0.1"
    assert set(payload["response_digests"]) == {
        "positions",
        "account",
        "symbol_config",
        "exchange_info",
        "long_short_ratio",
        "premium_index",
        "history",
    }


def test_live_public_sentiment_is_explicit_and_persisted_for_testnet(database):
    clock = Clock()
    sentiment = LiveSentiment(clock)
    provider, _, public, _, _ = build(database, sentiment=sentiment)
    result = provider(signal())
    assert result["evidence"]["sentiment_environment"] == "LIVE"
    assert all(
        path != "/futures/data/globalLongShortAccountRatio" for path, _ in public.calls
    )
    assert sentiment.calls == [
        (
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": "BTCUSDT", "period": "1h", "limit": 3},
        )
    ]
    with database() as conn:
        payload = conn.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (result["evidence"]["state_id"],),
        ).fetchone()[0]
    assert payload["market_sources"] == {
        "contract_environment": "SANDBOX",
        "sentiment_environment": "LIVE",
    }


@pytest.mark.parametrize(
    "kind,value",
    [("PULSE_UP", "5.25"), ("TREND_UP", "4"), ("VIOLENT_BULLISH", "18")],
)
def test_expected_move_uses_original_signal_fact(kind, value):
    assert expected_move(signal(kind)) == value
    with pytest.raises(ValueError, match="EVIDENCE_MISSING"):
        expected_move({**signal(kind), "features": {}})


@pytest.mark.parametrize(
    "defect",
    [
        "existing",
        "changed",
        "ratio_stale",
        "funding_stale",
        "rules",
        "history",
        "history_stale",
        "permission",
    ],
)
def test_incomplete_or_racy_sources_fail_before_context_is_returned(database, defect):
    provider, request, public, clock, _ = build(database)
    if defect == "existing":
        request.positions[0].append(
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}
        )
    elif defect == "changed":
        request.positions[1][0]["positionAmt"] = "0.2"
    elif defect == "ratio_stale":
        public.ratio_time = clock.now - 7200001
    elif defect == "funding_stale":
        public.funding_time = clock.now - 20000
    elif defect == "rules":
        public.exchange["symbols"][0]["filters"].pop()
    elif defect == "history_stale":
        provider.history = lambda _: {
            "stats": historical_stats(),
            "evidence": {"source": "old"},
            "observed_at_ms": clock.now - 120001,
            "valid_until_ms": clock.now + 1,
        }
    elif defect == "permission":
        request.account["canTrade"] = False
    else:
        provider.history = lambda _: {}
    with pytest.raises((ValueError, TypeError, KeyError)):
        provider(signal())
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='directional-account-context-v1'"
            ).fetchone()[0]
            == 0
        )
