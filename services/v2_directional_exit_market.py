"""Fresh credential-free Binance observations for directional exit decisions."""

from decimal import Decimal, localcontext

from v2_core.directional import number
from v2_core.ingress import milliseconds, symbol
from v2_core.ledger import amount


class BinanceDirectionalExitMarket:
    def __init__(
        self,
        transport,
        *,
        environment,
        clock_ms,
        exit_fee_rate,
        max_skew_ms=5000,
    ):
        if (
            environment != "SANDBOX"
            or getattr(transport, "environment", environment) != environment
            or not callable(transport)
            or not callable(clock_ms)
            or type(max_skew_ms) is not int
            or not 1 <= max_skew_ms <= 10000
        ):
            raise ValueError("explicit bounded Testnet exit market required")
        self.transport, self.environment, self.clock = (
            transport,
            environment,
            clock_ms,
        )
        self.fee = number(exit_fee_rate, minimum=0, maximum=Decimal(".01"))
        self.max_skew = max_skew_ms

    @staticmethod
    def _exact(value):
        encoded = format(value.normalize(), "f")
        amount(encoded)
        return encoded

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
        if close_15m < 3600000 or close_1h < 72000000:
            raise ValueError("insufficient exit market history")
        closes_15m = self._candles(
            target, interval="15m", count=4, end=close_15m, width=900000
        )
        closes_1h = self._candles(
            target, interval="1h", count=20, end=close_1h, width=3600000
        )
        with localcontext() as ctx:
            ctx.prec = 100
            ema9 = sum(closes_1h[-9:], Decimal(0)) / 9
            ema20 = sum(closes_1h, Decimal(0)) / 20
        return {
            "symbol": target,
            "environment": self.environment,
            "observed_at_ms": observed,
            "mark_price": self._exact(mark),
            "funding_rate": self._exact(funding),
            "ema9_1h": self._exact(ema9),
            "ema20_1h": self._exact(ema20),
            "momentum_closes_15m": [self._exact(value) for value in closes_15m],
            "exit_fee_rate": self._exact(self.fee),
            "source": "binance-public-exit-v1",
            "exchange_server_time_ms": server,
            "closed_15m_at_ms": close_15m,
            "closed_1h_at_ms": close_1h,
        }
