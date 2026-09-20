"""Credential-free, GET-only Binance time/closed-minute transport and PG quota."""

import json
from http.client import HTTPSConnection
from typing import ClassVar
from urllib.parse import urlencode

from v2_core.ingress import identity, milliseconds, symbol


class PublicMarketError(RuntimeError):
    """Fixed diagnostic code only; never raw endpoint responses."""


class PublicRateBudget:
    def __init__(self, connection_factory, *, scope, limit):
        identity(scope)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("explicit positive weight budget required")
        self._connect, self.scope, self.limit = connection_factory, scope, limit

    def permit(self, weight):
        if type(weight) is not int or not 1 <= weight <= 10:
            raise ValueError("bounded request weight required")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO v2_public_market_budgets(scope,weight_limit) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (self.scope, self.limit),
            )
            row = conn.execute(
                """SELECT weight_limit,window_id,used_weight,
                blocked_until>clock_timestamp(),floor(extract(epoch FROM clock_timestamp())/60)::bigint
                FROM v2_public_market_budgets WHERE scope=%s FOR UPDATE""",
                (self.scope,),
            ).fetchone()
            limit, window, used, blocked, now = row
            if limit != self.limit:
                raise ValueError("shared budget configuration conflict")
            used = used if window == now else 0
            if now < window or blocked or used + weight > limit:
                return False
            conn.execute(
                "UPDATE v2_public_market_budgets SET window_id=%s,used_weight=%s WHERE scope=%s",
                (now, used + weight, self.scope),
            )
        return True

    def penalize(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 259200:
            raise ValueError("bounded quota cooldown required")
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE v2_public_market_budgets SET blocked_until=GREATEST(blocked_until,
                clock_timestamp()+make_interval(secs=>%s)) WHERE scope=%s RETURNING scope""",
                (seconds, self.scope),
            ).fetchone()
            if row is None:
                raise RuntimeError("missing quota reservation")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response field")
        result[key] = value
    return result


class BinancePublicMarket:
    HOSTS: ClassVar = {"LIVE": "fapi.binance.com", "SANDBOX": "demo-fapi.binance.com"}

    def __init__(
        self,
        *,
        environment,
        budget,
        enabled=False,
        timeout=5,
        connection_factory=HTTPSConnection,
    ):
        if (
            environment not in self.HOSTS
            or type(enabled) is not bool
            or type(timeout) is not int
            or not 1 <= timeout <= 10
        ):
            raise ValueError("explicit environment and bounded public timeout required")
        self.environment, self.budget, self.enabled = environment, budget, enabled
        self.timeout, self._connection = timeout, connection_factory

    def __call__(self, path, params):
        if not self.enabled:
            raise PublicMarketError("PUBLIC_MARKET_DISABLED")
        if not isinstance(params, dict):
            raise TypeError("normalized public parameters required")
        if path == "/fapi/v1/time" and params == {}:
            weight = 1
        elif path == "/fapi/v1/klines" and set(params) == {
            "symbol",
            "interval",
            "startTime",
            "endTime",
            "limit",
        }:
            symbol(params["symbol"])
            start, end = (
                milliseconds(params["startTime"]),
                milliseconds(params["endTime"]),
            )
            if (
                params["interval"] != "1m"
                or type(params["limit"]) is not int
                or params["limit"] != 1440
                or start % 60000
                or end + 1 - start != 86400000
            ):
                raise ValueError("bounded closed-minute request required")
            weight = 10
        else:
            raise PublicMarketError("PUBLIC_ENDPOINT_DISABLED")
        if self.budget.permit(weight) is not True:
            raise PublicMarketError("QUOTA_DENIED")
        conn = None
        try:
            conn = self._connection(self.HOSTS[self.environment], timeout=self.timeout)
            conn.request(
                "GET",
                path + ("?" + urlencode(params) if params else ""),
                headers={"Accept": "application/json"},
            )
            response = conn.getresponse()
            if response.status in (418, 429):
                retry = response.getheader("Retry-After", "")
                floor = 86400 if response.status == 418 else 60
                delay = (
                    min(259200, max(floor, int(retry)))
                    if isinstance(retry, str)
                    and retry.isascii()
                    and retry.isdigit()
                    and len(retry) <= 8
                    else floor
                )
                self.budget.penalize(delay)
                raise PublicMarketError("RATE_LIMITED")
            if response.status != 200:
                raise PublicMarketError("PUBLIC_HTTP_ERROR")
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise PublicMarketError("PUBLIC_RESPONSE_TOO_LARGE")
            result = json.loads(
                body,
                object_pairs_hook=unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("nonfinite response")
                ),
            )
            if (
                not isinstance(result, (dict, list))
                or isinstance(result, dict)
                and "code" in result
            ):
                raise PublicMarketError("PUBLIC_API_ERROR")
            return result
        except PublicMarketError:
            raise
        except Exception:  # noqa: BLE001 - never expose network response details
            raise PublicMarketError("PUBLIC_TRANSPORT_FAILURE") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - cleanup cannot replace request result
                    pass
