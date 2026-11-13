from decimal import Decimal, localcontext

import pytest

from services.v2_directional_exit_market import (
    EMA_EXIT_INDICATORS,
    BinanceDirectionalExitMarket,
    closed_hour_ema,
)


class Market:
    environment = "SANDBOX"
    now = 100 * 3600000

    def __init__(self):
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, params))
        if path.endswith("/time"):
            return {"serverTime": self.now}
        if path.endswith("/premiumIndex"):
            return {
                "symbol": "BTCUSDT",
                "markPrice": "120",
                "lastFundingRate": "0",
                "time": self.now,
            }
        width = 3600000 if params["interval"] == "1h" else 900000
        result = []
        for i in range(params["limit"]):
            price = "100" if i < params["limit"] - 1 else "120"
            start = params["startTime"] + i * width
            result.append(
                [
                    start,
                    price,
                    price,
                    price,
                    price,
                    "1",
                    start + width - 1,
                    "1",
                    1,
                    "1",
                    "1",
                    "0",
                ]
            )
        return result


def test_real_closed_hour_ema_has_new_version_and_no_future_request():
    market = Market()
    result = BinanceDirectionalExitMarket(
        market,
        environment="SANDBOX",
        clock_ms=lambda: market.now,
        exit_fee_rate=".0004",
        indicator_version=EMA_EXIT_INDICATORS,
    )("BTCUSDT")
    assert result["source"] == EMA_EXIT_INDICATORS
    assert result["ema9_1h"] == "104"
    assert Decimal(result["ema20_1h"]) == Decimal("101.904761904761904762")
    assert result["indicator_contract"]["closed_bars"] == 60
    hours = market.calls[-1][1]
    assert hours["interval"] == "1h" and hours["limit"] == 60
    assert hours["endTime"] < market.now


def test_legacy_default_keeps_sma_and_output_shape():
    market = Market()
    result = BinanceDirectionalExitMarket(
        market,
        environment="SANDBOX",
        clock_ms=lambda: market.now,
        exit_fee_rate="0",
    )("BTCUSDT")
    assert result["source"] == "binance-public-exit-v1"
    assert result["ema20_1h"] == "101"
    assert "indicator_contract" not in result
    assert market.calls[-1][1]["limit"] == 24


def test_ema_ignores_callers_decimal_precision():
    closes = ["100"] * 59 + ["120"]
    expected = closed_hour_ema(closes, 9)
    with localcontext() as ctx:
        ctx.prec = 2
        assert closed_hour_ema(closes, 9) == expected


@pytest.mark.parametrize("count", [0, 20, 59, 61])
def test_ema_requires_declared_seed_history(count):
    with pytest.raises(ValueError, match="WARMUP"):
        closed_hour_ema(["1"] * count, 20)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "0", "-1"])
def test_ema_rejects_bad_prices(bad):
    with pytest.raises(ValueError):
        closed_hour_ema(["1"] * 59 + [bad], 9)


def test_unrecognized_contract_rejected_before_network():
    market = Market()
    with pytest.raises(ValueError):
        BinanceDirectionalExitMarket(
            market,
            environment="SANDBOX",
            clock_ms=lambda: market.now,
            exit_fee_rate="0",
            indicator_version="auto",
        )
    assert not market.calls
