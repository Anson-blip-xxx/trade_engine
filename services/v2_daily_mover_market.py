"""Independent public LIVE market reader, never an exchange order client."""

import json
import time
from http.client import HTTPSConnection
from urllib.parse import urlencode

from v2_core.daily_movers import DAY, MINUTE, summarize_day


class MoverMarketError(RuntimeError):
    pass


def public_symbol(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 44
        or not value.endswith("USDT")
        or any(not (c.isalnum() or c == "_") for c in value)
    ):
        raise ValueError("MOVER_PUBLIC_SYMBOL_INVALID")
    return value


class MoverMarket:
    """GET allowlist + shared host reserve + small additional research pace.

    No credentials, redirects, arbitrary URLs, POST, or trading operations.
    Every attempted request burns shared quota; 429/418 defers the entire job.
    """

    def __init__(
        self,
        budget,
        *,
        request_spacing=0.6,
        max_seconds=900,
        factory=HTTPSConnection,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ):
        if not 0.5 <= request_spacing <= 5 or not 30 <= max_seconds <= 1800:
            raise ValueError("MOVER_MARKET_LIMIT_INVALID")
        self.budget, self.factory = budget, factory
        self.spacing, self.monotonic, self.sleep = request_spacing, monotonic, sleep
        self.started = monotonic()
        self.deadline = self.started + max_seconds
        self.next_at = self.started
        self.requests = 0
        self.weight = 0

    def get(self, path, params):
        if path in ("/fapi/v1/time", "/fapi/v1/exchangeInfo") and params == {}:
            weight = 1
        elif path == "/fapi/v1/klines" and set(params) == {
            "symbol",
            "interval",
            "startTime",
            "endTime",
            "limit",
        }:
            public_symbol(params["symbol"])
            if (
                params["interval"] not in ("1m", "1h")
                or type(params["limit"]) is not int
                or not 1 <= params["limit"] <= 480
                or any(type(params[k]) is not int for k in ("startTime", "endTime"))
                or not 0 <= params["startTime"] < params["endTime"] < 2**53
                or params["endTime"] - params["startTime"] > DAY
            ):
                raise ValueError("MOVER_KLINE_BOUNDS")
            weight = 1 if params["limit"] < 100 else 2
        else:
            raise ValueError("MOVER_PUBLIC_ENDPOINT_DISABLED")
        delay = max(0, self.next_at - self.monotonic())
        if self.monotonic() + delay + 5 > self.deadline:
            raise MoverMarketError("MOVER_JOB_DEADLINE")
        if delay:
            self.sleep(delay)
        if not self.budget.permit(weight):
            raise MoverMarketError("MOVER_SHARED_QUOTA_WAIT")
        if self.monotonic() + 5 > self.deadline:
            raise MoverMarketError("MOVER_JOB_DEADLINE")
        self.requests += 1
        self.weight += weight
        # Pace by weight, not just requests. At most100 research weight/min.
        self.next_at = self.monotonic() + self.spacing * weight
        conn = None
        try:
            conn = self.factory("fapi.binance.com", timeout=5)
            suffix = "?" + urlencode(params) if params else ""
            conn.request("GET", path + suffix)
            response = conn.getresponse()
            if response.status in (418, 429):
                raw = response.getheader("Retry-After", "60")
                seconds = (
                    min(259200, max(60, int(raw)))
                    if len(raw) <= 8 and raw.isdecimal()
                    else 60
                )
                try:
                    self.budget.penalize(seconds)
                except Exception:  # noqa: BLE001, S110 - preserve quota halt on DB failure
                    pass  # the whole research job still stops on HTTP rate limit
                raise MoverMarketError("MOVER_RATE_LIMITED")
            if response.status != 200:
                raise MoverMarketError("MOVER_PUBLIC_HTTP_ERROR")
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise MoverMarketError("MOVER_RESPONSE_TOO_LARGE")
            value = json.loads(raw)
            if isinstance(value, dict) and "code" in value:
                raise MoverMarketError("MOVER_PUBLIC_API_ERROR")
            return value
        except MoverMarketError:
            raise
        except Exception:  # noqa: BLE001 - public transport errors have a fixed diagnostic
            raise MoverMarketError("MOVER_PUBLIC_TRANSPORT_FAILURE") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - cleanup cannot replace outcome
                    pass  # cleanup cannot mask a quota/transport result

    def catalog(self):
        value = self.get("/fapi/v1/exchangeInfo", {})
        if not isinstance(value, dict) or not isinstance(value.get("symbols"), list):
            raise MoverMarketError("MOVER_CATALOG_INVALID")
        if not 1 <= len(value["symbols"]) <= 5000:
            raise MoverMarketError("MOVER_CATALOG_BOUND")
        rows = []
        for row in value["symbols"]:
            if not isinstance(row, dict):
                raise MoverMarketError("MOVER_CATALOG_INVALID")
            if (
                row.get("quoteAsset") != "USDT"
                or row.get("contractType") != "PERPETUAL"
            ):
                continue
            public_symbol(row.get("symbol"))
            rows.append(
                {
                    k: row.get(k)
                    for k in (
                        "symbol",
                        "quoteAsset",
                        "contractType",
                        "onboardDate",
                        "deliveryDate",
                        "status",
                    )
                }
            )
        if not rows or len({r["symbol"] for r in rows}) != len(rows):
            raise MoverMarketError("MOVER_CATALOG_DUPLICATE_OR_EMPTY")
        return rows

    def bars(self, symbol, start_ms, end_ms, *, interval_minutes=60):
        public_symbol(symbol)
        if (
            type(interval_minutes) is not int
            or interval_minutes not in (1, 60)
            or type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms < 2**53
            or (start_ms + 8 * 3600000) % DAY
            or end_ms - start_ms != DAY
        ):
            raise ValueError("MOVER_CLOSED_DAY_REQUIRED")
        step = interval_minutes * MINUTE
        rows = []
        for start in range(start_ms, end_ms, step * 480):
            end = min(end_ms, start + step * 480)
            batch = self.get(
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": "1h" if interval_minutes == 60 else "1m",
                    "startTime": start,
                    "endTime": end - 1,
                    "limit": (end - start) // step,
                },
            )
            if not isinstance(batch, list) or len(batch) > 480:
                raise MoverMarketError("MOVER_KLINES_INVALID")
            for bar in batch:
                if (
                    not isinstance(bar, list)
                    or len(bar) < 7
                    or type(bar[0]) is not int
                    or type(bar[6]) is not int
                    or not start <= bar[0] < end
                    or bar[6] != bar[0] + step - 1
                ):
                    raise MoverMarketError("MOVER_KLINES_INVALID")
                rows.append(
                    dict(zip(("t", "o", "h", "l", "c", "v"), bar[:6], strict=True))
                )
        # The core checks missing, duplicate, future, malformed OHLC and volume.
        return rows

    def summary(self, target, *, start_ms, end_ms, as_of_ms):
        if type(as_of_ms) is not int or as_of_ms < end_ms:
            raise ValueError("MOVER_DAY_NOT_CLOSED")
        rows = self.bars(target, start_ms, end_ms)
        return summarize_day(
            rows,
            symbol=target,
            start_ms=start_ms,
            end_ms=end_ms,
            as_of_ms=as_of_ms,
            interval_minutes=60,
        ), rows
