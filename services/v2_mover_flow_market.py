"""Bounded public LIVE evidence reads for completed UTC+8 mover days.

This is independent of order clients and credentials. Catalog identity describes
the current exchange catalogs, not proof of historical spot availability. The
core validates numerical values, hourly coverage and settlement attribution.
"""

import json
import time
from http.client import HTTPSConnection
from urllib.parse import urlencode

from services.v2_daily_mover_market import MoverMarketError, public_symbol
from v2_core.daily_movers import DAY

VERSION = "mover-flow-public-v1"
MAPPING_BASIS = "CURRENT_EXACT_SYMBOL_BASE_QUOTE"
MAX_REQUESTS = 180
_UTC8 = 8 * 3_600_000
_SPOT_CATALOG = "/api/v3/exchangeInfo"
_FUTURES_CATALOG = "/fapi/v1/exchangeInfo"
_SPOT_KLINES = "/api/v3/klines"
_OPEN_INTEREST = "/futures/data/openInterestHist"
_FUNDING = "/fapi/v1/fundingRate"

# Binance's market_data_only FAQ explicitly allows both spot endpoints on this
# host. The catalog weight is 20; spot klines are 2. OI (venue weight 0) and
# funding (500 calls/5min) each consume at least one shared internal permit.
# Suppress unused spot permission-set arrays so the complete identity catalog
# fits the existing byte cap. This changes no symbols or trading-status filter.
# https://developers.binance.com/en/docs/products/spot/faqs/market_data_only
# https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general
# https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
_ENDPOINTS = {
    _SPOT_CATALOG: ("data-api.binance.vision", 20, 8_000_000),
    _FUTURES_CATALOG: ("fapi.binance.com", 1, 4_000_000),
    _SPOT_KLINES: ("data-api.binance.vision", 2, 128_000),
    _OPEN_INTEREST: ("fapi.binance.com", 1, 128_000),
    _FUNDING: ("fapi.binance.com", 1, 128_000),
}


class _ComponentFailure(MoverMarketError):
    """Sanitized public-data failure; other components may still be read."""


def _wall_ms():
    return int(time.time() * 1000)


def _asset(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 44
        and all(c.isalnum() or c == "_" for c in value)
    )


class FlowMarket:
    """Fixed public GETs, shared quota per attempt, and one instance/job budget.

    No redirects, authentication, order endpoints, retries or arbitrary URLs.
    Weight pacing is at least 0.5 seconds per weight, across both hosts. The
    lifetime cap of 180 requests is also below OI's 1000/5min and funding's
    500/5min endpoint limits. Shared quota/rate-limit/deadline failures latch a
    halt; ordinary HTTP, transport and shape failures become component UNKNOWN.
    """

    def __init__(
        self,
        budget,
        *,
        factory=HTTPSConnection,
        clock_ms=_wall_ms,
        monotonic=time.monotonic,
        sleep=time.sleep,
        request_spacing=0.6,
        max_seconds=300,
    ):
        if (
            type(request_spacing) not in (int, float)
            or not 0.5 <= request_spacing <= 5
            or type(max_seconds) not in (int, float)
            or not 30 <= max_seconds <= 300
        ):
            raise ValueError("MOVER_FLOW_LIMIT_INVALID")
        self.budget, self.factory, self.clock_ms = budget, factory, clock_ms
        self.monotonic, self.sleep, self.spacing = monotonic, sleep, request_spacing
        self.started = monotonic()
        self.deadline, self.next_at = self.started + max_seconds, self.started
        self.requests = self.weight = 0
        self._halt_reason = None
        self._catalogs = {}

    def _halt(self, reason):
        self._halt_reason = reason
        raise MoverMarketError(reason) from None

    def _active(self, *, reserve=0):
        if self._halt_reason:
            raise MoverMarketError(self._halt_reason)
        if self.monotonic() + reserve > self.deadline:
            self._halt("MOVER_JOB_DEADLINE")

    def _closed_day(self, start_ms, end_ms):
        now = self.clock_ms()
        if type(now) is not int or not 0 <= now < 2**53:
            self._halt("MOVER_FLOW_CLOCK_INVALID")
        today = (now + _UTC8) // DAY * DAY - _UTC8
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms < 2**53
            or (start_ms + _UTC8) % DAY
            or end_ms - start_ms != DAY
        ):
            raise ValueError("MOVER_CLOSED_DAY_REQUIRED")
        if end_ms > today:
            raise ValueError("MOVER_DAY_NOT_CLOSED")
        if start_ms < today - 3 * DAY:
            raise ValueError("MOVER_FLOW_RECENT_DAY_REQUIRED")

    def _request_spec(self, path, params):
        if (
            not isinstance(path, str)
            or path not in _ENDPOINTS
            or not isinstance(params, dict)
        ):
            raise ValueError("MOVER_PUBLIC_ENDPOINT_DISABLED")
        if path == _SPOT_CATALOG:
            if params != {"showPermissionSets": "false"}:
                raise ValueError("MOVER_PUBLIC_ENDPOINT_DISABLED")
        elif path == _FUTURES_CATALOG:
            if params:
                raise ValueError("MOVER_PUBLIC_ENDPOINT_DISABLED")
        else:
            fields = {"symbol", "startTime", "endTime", "limit"}
            if path == _SPOT_KLINES:
                fields.add("interval")
            elif path == _OPEN_INTEREST:
                fields.add("period")
            if set(params) != fields:
                raise ValueError("MOVER_PUBLIC_ENDPOINT_DISABLED")
            public_symbol(params["symbol"])
            if (
                any(
                    type(params[key]) is not int
                    for key in ("startTime", "endTime", "limit")
                )
                or not 0 <= params["startTime"] < params["endTime"] < 2**53
            ):
                raise ValueError("MOVER_FLOW_REQUEST_BOUNDS")
            start, end = params["startTime"], params["endTime"]
            if path == _SPOT_KLINES:
                if params["interval"] != "1h" or params["limit"] != 24:
                    raise ValueError("MOVER_FLOW_REQUEST_BOUNDS")
                self._closed_day(start, end + 1)
            elif path == _OPEN_INTEREST:
                if params["period"] != "1h" or params["limit"] != 30:
                    raise ValueError("MOVER_FLOW_REQUEST_BOUNDS")
                self._closed_day(start, end)
            else:
                if params["limit"] != 200:
                    raise ValueError("MOVER_FLOW_REQUEST_BOUNDS")
                self._closed_day(start + DAY, end)
        return _ENDPOINTS[path]

    def get(self, path, params):
        """Read one exact allowlisted request; all errors have fixed diagnostics."""
        host, weight, cap = self._request_spec(path, params)
        self._active()
        if self.requests >= MAX_REQUESTS:
            self._halt("MOVER_FLOW_REQUEST_LIMIT")
        delay = max(0, self.next_at - self.monotonic())
        self._active(reserve=delay + 5)
        if delay:
            self.sleep(delay)
        self._active(reserve=5)
        try:
            permitted = self.budget.permit(weight)
        except Exception:  # noqa: BLE001 - never expose shared-budget internals
            self._halt("MOVER_SHARED_QUOTA_FAILURE")
        if not permitted:
            self._halt("MOVER_SHARED_QUOTA_WAIT")
        self._active(reserve=5)
        self.requests += 1
        self.weight += weight
        self.next_at = self.monotonic() + self.spacing * weight
        connection = None
        try:
            connection = self.factory(host, timeout=5)
            suffix = "?" + urlencode(params) if params else ""
            connection.request("GET", path + suffix)
            response = connection.getresponse()
            if response.status in (418, 429):
                try:
                    raw_retry = response.getheader("Retry-After", "60")
                except Exception:  # noqa: BLE001 - broken headers cannot erase a ban
                    raw_retry = "60"
                seconds = (
                    min(259200, max(60, int(raw_retry)))
                    if isinstance(raw_retry, str)
                    and len(raw_retry) <= 8
                    and raw_retry.isdecimal()
                    else 60
                )
                try:
                    self.budget.penalize(seconds)
                except Exception:  # noqa: BLE001, S110 - preserve rate-limit halt
                    pass
                self._halt("MOVER_RATE_LIMITED")
            self._active()
            if response.status != 200:
                raise _ComponentFailure("MOVER_PUBLIC_HTTP_ERROR")
            raw = response.read(cap + 1)
            self._active()
            if len(raw) > cap:
                raise _ComponentFailure("MOVER_RESPONSE_TOO_LARGE")
            value = json.loads(raw)
            self._active()
            if isinstance(value, dict) and "code" in value:
                raise _ComponentFailure("MOVER_PUBLIC_API_ERROR")
            return value
        except MoverMarketError:
            raise
        except Exception:  # noqa: BLE001 - response/transport details are untrusted
            self._active()
            raise _ComponentFailure("MOVER_PUBLIC_TRANSPORT_FAILURE") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001, S110 - cleanup cannot mask outcome
                    pass

    def _catalog(self, path):
        if path not in self._catalogs:
            try:
                value = self.get(
                    path,
                    {"showPermissionSets": "false"} if path == _SPOT_CATALOG else {},
                )
                rows = value.get("symbols") if isinstance(value, dict) else None
                cap = 10_000 if path == _SPOT_CATALOG else 5_000
                if not isinstance(rows, list) or not 1 <= len(rows) <= cap:
                    raise _ComponentFailure("MOVER_FLOW_CATALOG_INVALID")
                index = {}
                for row in rows:
                    if not isinstance(row, dict) or not all(
                        _asset(row.get(key))
                        for key in ("symbol", "baseAsset", "quoteAsset")
                    ):
                        raise _ComponentFailure("MOVER_FLOW_CATALOG_INVALID")
                    if path == _FUTURES_CATALOG and not _asset(row.get("contractType")):
                        raise _ComponentFailure("MOVER_FLOW_CATALOG_INVALID")
                    if row.get("quoteAsset") != "USDT":
                        continue
                    if (
                        path == _FUTURES_CATALOG
                        and row.get("contractType") != "PERPETUAL"
                    ):
                        continue
                    try:
                        symbol = public_symbol(row.get("symbol"))
                    except ValueError:
                        raise _ComponentFailure("MOVER_FLOW_CATALOG_INVALID") from None
                    if symbol in index:
                        raise _ComponentFailure("MOVER_FLOW_CATALOG_INVALID")
                    index[symbol] = (row["baseAsset"], row["quoteAsset"])
                self._catalogs[path] = (index, None)
            except _ComponentFailure as exc:
                # Cache failures too: one venue catalog attempt for this job,
                # never a failed catalog call repeated for every candidate.
                self._catalogs[path] = (None, str(exc))
        return self._catalogs[path]

    def _mapping(self, symbol):
        futures, futures_error = self._catalog(_FUTURES_CATALOG)
        spot, spot_error = self._catalog(_SPOT_CATALOG)
        result = {
            "status": "UNKNOWN",
            "basis": MAPPING_BASIS,
            "spot_symbol": None,
            "base_asset": None,
            "quote_asset": None,
        }
        if futures_error or spot_error:
            result["reason"] = futures_error or spot_error
        elif symbol not in futures or symbol not in spot:
            result["reason"] = "NO_EXACT_SPOT_MAPPING"
        elif futures[symbol] != spot[symbol]:
            result["reason"] = "EXACT_SPOT_ASSET_MISMATCH"
        else:
            result.update(
                status="MATCHED",
                spot_symbol=symbol,
                base_asset=futures[symbol][0],
                quote_asset=futures[symbol][1],
            )
        return result

    @staticmethod
    def _rows(value, kind):
        cap = {"spot": 24, "open_interest": 30, "funding": 200}[kind]
        if not isinstance(value, list) or len(value) > cap:
            raise _ComponentFailure("MOVER_FLOW_ROWS_INVALID")
        if not value:
            raise _ComponentFailure("MOVER_FLOW_ROWS_EMPTY")
        for row in value:
            if kind == "spot":
                valid = (
                    isinstance(row, list)
                    and len(row) == 12
                    and all(type(row[i]) is int for i in (0, 6, 8))
                    and all(
                        isinstance(row[i], str) and 1 <= len(row[i]) <= 128
                        for i in (1, 2, 3, 4, 5, 7, 9, 10, 11)
                    )
                )
            else:
                stamp = "timestamp" if kind == "open_interest" else "fundingTime"
                numbers = (
                    ("sumOpenInterest", "sumOpenInterestValue")
                    if kind == "open_interest"
                    else ("fundingRate", "markPrice")
                )
                optional = (
                    {"CMCCirculatingSupply"}
                    if kind == "open_interest"
                    else {"rateType"}
                )
                required = {"symbol", stamp, *numbers}
                valid = (
                    isinstance(row, dict)
                    and required <= row.keys() <= required | optional
                    and type(row[stamp]) is int
                    and _asset(row["symbol"])
                    and all(
                        isinstance(row[key], str) and 1 <= len(row[key]) <= 128
                        for key in row.keys() - {"symbol", stamp}
                    )
                )
            if not valid:
                raise _ComponentFailure("MOVER_FLOW_ROWS_INVALID")
        return value

    def _component(self, kind, path, params):
        result = {
            "status": "UNKNOWN",
            "source": "BINANCE_SPOT_LIVE" if kind == "spot" else "BINANCE_USDM_LIVE",
            "fetched_at_ms": self.clock_ms(),
            "rows": [],
        }
        try:
            result["rows"] = self._rows(self.get(path, params), kind)
            result["status"] = "OBSERVED"
        except _ComponentFailure as exc:
            result["reason"] = str(exc)
        result["fetched_at_ms"] = self.clock_ms()
        return result

    def collect(self, symbol, *, start_ms, end_ms):
        """Return original public observations for one of the last 3 closed days.

        The OI query includes the day-end sample. Funding includes the preceding
        day so the core can inspect the interval preceding an in-day settlement.
        Empty/malformed/failed components are UNKNOWN, never synthetic zeroes.
        """
        public_symbol(symbol)
        self._closed_day(start_ms, end_ms)
        self._active()
        mapping = self._mapping(symbol)
        if mapping["status"] == "MATCHED":
            spot = self._component(
                "spot",
                _SPOT_KLINES,
                {
                    "symbol": symbol,
                    "interval": "1h",
                    "startTime": start_ms,
                    "endTime": end_ms - 1,
                    "limit": 24,
                },
            )
        else:
            spot = {
                "status": "UNKNOWN",
                "reason": mapping["reason"],
                "source": "BINANCE_SPOT_LIVE",
                "fetched_at_ms": self.clock_ms(),
                "rows": [],
            }
        open_interest = self._component(
            "open_interest",
            _OPEN_INTEREST,
            {
                "symbol": symbol,
                "period": "1h",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 30,
            },
        )
        funding = self._component(
            "funding",
            _FUNDING,
            {
                "symbol": symbol,
                "startTime": start_ms - DAY,
                "endTime": end_ms,
                "limit": 200,
            },
        )
        self._active()
        return {
            "version": VERSION,
            "source": "BINANCE_LIVE_PUBLIC",
            "symbol": symbol,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "mapping": mapping,
            "spot": spot,
            "open_interest": open_interest,
            "funding": funding,
        }
