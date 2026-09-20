"""Bounded HMAC transport. No automatic retry, redirects, config reads or logs."""

import hashlib
import hmac
import json
from http.client import HTTPSConnection
from typing import ClassVar
from urllib.parse import urlencode


class ExchangeTransportError(RuntimeError):
    """Safe diagnostic only: never contains response text, URL or credentials."""

    def __init__(self, category, *, status=None, code=None, retry_after=None):
        super().__init__(category)
        self.status, self.code, self.retry_after = status, code, retry_after


class BinanceSignedTransport:
    _HOSTS: ClassVar = {"LIVE": "fapi.binance.com", "SANDBOX": "demo-fapi.binance.com"}
    _READS = frozenset(
        {"/fapi/v1/order", "/fapi/v1/userTrades", "/fapi/v1/positionSide/dual"}
    )

    def __init__(
        self,
        *,
        account_id,
        environment,
        api_key,
        api_secret,
        clock_ms,
        permit,
        enable_trading=False,
        timeout=10,
        connection_factory=HTTPSConnection,
    ):
        if (
            environment not in self._HOSTS
            or not isinstance(account_id, str)
            or not account_id
        ):
            raise ValueError("explicit account and environment required")
        if not all(
            isinstance(v, str) and v and v.isascii() and not any(c.isspace() for c in v)
            for v in (api_key, api_secret)
        ):
            raise ValueError("nonempty ASCII HMAC credentials required")
        if (
            not callable(clock_ms)
            or not callable(permit)
            or not callable(connection_factory)
        ):
            raise TypeError(
                "explicit clock, quota permit and connection factory required"
            )
        if (
            type(enable_trading) is not bool
            or type(timeout) is not int
            or not 1 <= timeout <= 30
        ):
            raise ValueError("explicit write gate and bounded timeout required")
        self.account_id, self.environment = account_id, environment
        self._key, self._secret = api_key, api_secret
        self._clock, self._permit, self._connection = (
            clock_ms,
            permit,
            connection_factory,
        )
        self._trading, self._timeout = enable_trading, timeout

    def __call__(self, method, path, params):
        if (
            method == "GET"
            and path in self._READS
            or method == "POST"
            and path == "/fapi/v1/order"
            and self._trading
        ):
            pass
        else:
            raise ExchangeTransportError("ENDPOINT_OR_WRITE_DISABLED")
        if not isinstance(params, dict) or any(
            k in params for k in ("timestamp", "signature", "recvWindow")
        ):
            raise ValueError("caller cannot override signing fields")
        if any(
            not isinstance(k, str) or type(v) not in (str, int)
            for k, v in params.items()
        ):
            raise ValueError("normalized scalar request parameters required")
        now = self._clock()
        if type(now) is not int or now < 0:
            raise ValueError("millisecond clock required")
        if self._permit(method, path) is not True:
            raise ExchangeTransportError("QUOTA_DENIED")
        payload = urlencode({**params, "timestamp": now, "recvWindow": 5000})
        signature = hmac.new(
            self._secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        signed = payload + "&signature=" + signature
        conn = None
        try:
            conn = self._connection(
                self._HOSTS[self.environment], timeout=self._timeout
            )
            headers = {
                "X-MBX-APIKEY": self._key,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            target = path + "?" + signed if method == "GET" else path
            conn.request(
                method,
                target,
                body=signed if method == "POST" else None,
                headers=headers,
            )
            response = conn.getresponse()
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ExchangeTransportError("RESPONSE_TOO_LARGE")
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ExchangeTransportError("INVALID_RESPONSE") from None
            code = data.get("code") if isinstance(data, dict) else None
            code = code if type(code) is int else None
            if (
                response.status == 400
                and code == -2013
                and method == "GET"
                and path == "/fapi/v1/order"
            ):
                return {"code": -2013}
            if not 200 <= response.status < 300 or (code is not None and code < 0):
                retry = response.getheader("Retry-After", "")
                retry = (
                    min(int(retry), 86400)
                    if isinstance(retry, str)
                    and retry.isascii()
                    and retry.isdigit()
                    and len(retry) < 10
                    else None
                )
                raise ExchangeTransportError(
                    "EXCHANGE_RESPONSE_ERROR",
                    status=response.status,
                    code=code,
                    retry_after=retry,
                )
            if not isinstance(data, (dict, list)):
                raise ExchangeTransportError("INVALID_RESPONSE")
            return data
        except ExchangeTransportError:
            raise
        except Exception:  # noqa: BLE001 - remove possible request/credential-bearing transport detail
            raise ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN") from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001, S110 - cannot replace received response or leak credentials
                    pass
