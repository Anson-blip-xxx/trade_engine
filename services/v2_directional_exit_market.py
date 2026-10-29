"""Fresh credential-free Binance observations for directional exit decisions."""

from decimal import ROUND_HALF_EVEN, Decimal

from v2_core.directional import number
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount
from v2_core.precision import decimal_text, derived_amount, money_context

LEGACY_EXIT_INDICATORS = "binance-public-exit-v1"
EMA_EXIT_INDICATORS = "binance-public-exit-v2"


def closed_hour_ema(closes, period):
    """SMA seed followed by EMA updates, oldest closed hours first.

    This is deliberately separate from the legacy arithmetic-mean contract.
    A different seed/history is a different feature, not a silent replay fix.
    """
    if (
        type(period) is not int
        or period not in (9, 20)
        or not isinstance(closes, (list, tuple))
        or len(closes) != 60
    ):
        raise ValueError("EXIT_EMA_WARMUP_REQUIRED")
    values = [amount(str(value), positive=True) for value in closes]
    with money_context():
        result = sum(values[:period], Decimal(0)) / period
        alpha = Decimal(2) / (period + 1)
        for value in values[period:]:
            result += alpha * (value - result)
        return result


class BinanceDirectionalExitMarket:
    def __init__(
        self,
        transport,
        *,
        environment,
        clock_ms,
        exit_fee_rate,
        max_skew_ms=5000,
        indicator_version=LEGACY_EXIT_INDICATORS,
    ):
        if (
            environment != "SANDBOX"
            or getattr(transport, "environment", environment) != environment
            or not callable(transport)
            or not callable(clock_ms)
            or type(max_skew_ms) is not int
            or not 1 <= max_skew_ms <= 10000
            or indicator_version not in (LEGACY_EXIT_INDICATORS, EMA_EXIT_INDICATORS)
        ):
            raise ValueError("explicit bounded Testnet exit market required")
        self.transport, self.environment, self.clock = (
            transport,
            environment,
            clock_ms,
        )
        self.fee = number(exit_fee_rate, minimum=0, maximum=Decimal(".01"))
        self.max_skew = max_skew_ms
        self.indicator_version = indicator_version

    @staticmethod
    def _exact(value):
        return decimal_text(derived_amount(value, rounding=ROUND_HALF_EVEN))

    def _candles(self, target, *, interval, count, end, width):
        start = end - count * width
        rows = self.transport(
            "/fapi/v1/klines",
            {
                "symbol": target,
                "interval": interval,
                "startTime": start,
                "endTime": end - 1,
                "limit": count,
            },
        )
        if not isinstance(rows, list) or len(rows) != count:
            raise ValueError("incomplete closed exit candles")
        closes = []
        for index, row in enumerate(rows):
            opened = start + index * width
            if (
                not isinstance(row, list)
                or len(row) != 12
                or milliseconds(row[0]) != opened
                or milliseconds(row[6]) != opened + width - 1
                or not isinstance(row[4], str)
            ):
                raise ValueError("invalid closed exit candle")
            closes.append(amount(row[4], positive=True))
        return closes

    def __call__(self, raw_symbol):
        target = symbol(raw_symbol)
        before = milliseconds(self.clock())
        clock = self.transport("/fapi/v1/time", {})
        after = milliseconds(self.clock())
        if (
            not isinstance(clock, dict)
            or set(clock) != {"serverTime"}
            or after < before
        ):
            raise ValueError("invalid exit exchange clock")
        server = milliseconds(clock["serverTime"])
        if not before - self.max_skew <= server <= after + self.max_skew:
            raise ValueError("exit exchange clock outside accepted skew")
        premium = self.transport("/fapi/v1/premiumIndex", {"symbol": target})
        if (
            not isinstance(premium, dict)
            or premium.get("symbol") != target
            or not isinstance(premium.get("markPrice"), str)
            or not isinstance(premium.get("lastFundingRate"), str)
        ):
            raise ValueError("invalid exit premium observation")
        mark = amount(premium["markPrice"], positive=True)
        funding = number(premium["lastFundingRate"], minimum=-1, maximum=1)
        observed = milliseconds(premium.get("time"))
        if not server - self.max_skew <= observed <= after + self.max_skew:
            raise ValueError("stale or future exit premium observation")
        close_15m = server // 900000 * 900000
        close_1h = server // 3600000 * 3600000
        hour_count = 60 if self.indicator_version == EMA_EXIT_INDICATORS else 20
        if close_15m < 3600000 or close_1h < hour_count * 3600000:
            raise ValueError("insufficient exit market history")
        closes_15m = self._candles(
            target, interval="15m", count=4, end=close_15m, width=900000
        )
        closes_1h = self._candles(
            target, interval="1h", count=hour_count, end=close_1h, width=3600000
        )
        with money_context():
            if self.indicator_version == EMA_EXIT_INDICATORS:
                ema9, ema20 = (closed_hour_ema(closes_1h, p) for p in (9, 20))
            else:
                ema9 = sum(closes_1h[-9:], Decimal(0)) / 9
                ema20 = sum(closes_1h, Decimal(0)) / 20
        result = {
            "symbol": target,
            "environment": self.environment,
            "observed_at_ms": observed,
            "mark_price": self._exact(mark),
            "funding_rate": self._exact(funding),
            "ema9_1h": self._exact(ema9),
            "ema20_1h": self._exact(ema20),
            "momentum_closes_15m": [self._exact(value) for value in closes_15m],
            "exit_fee_rate": self._exact(self.fee),
            "source": self.indicator_version,
            "exchange_server_time_ms": server,
            "closed_15m_at_ms": close_15m,
            "closed_1h_at_ms": close_1h,
        }
        if self.indicator_version == EMA_EXIT_INDICATORS:
            result["indicator_contract"] = {
                "interval": "1h",
                "closed_bars": 60,
                "average": "EMA_SMA_SEED",
                "seed_history": "FIXED_60_CLOSED_HOURS",
                "periods": [9, 20],
            }
        return result
