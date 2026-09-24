"""Bounded HMAC transport. No automatic retry, redirects, config reads or logs."""

import hashlib
import hmac
import json
import re
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
        {
            "/fapi/v1/order",
            "/fapi/v1/userTrades",
            "/fapi/v1/positionSide/dual",
            "/fapi/v1/income",
            "/fapi/v3/account",
            "/fapi/v3/positionRisk",
            "/fapi/v1/openOrders",
            "/fapi/v1/openAlgoOrders",
            "/fapi/v1/algoOrder",
            "/fapi/v1/multiAssetsMargin",
            "/fapi/v1/accountConfig",
            "/fapi/v1/symbolConfig",
            "/fapi/v1/leverageBracket",
        }
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
        enable_testnet_cancellation=False,
        enable_testnet_order_cancellation=False,
        enable_testnet_protection=False,
        enable_testnet_settings=False,
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
            or type(enable_testnet_order_cancellation) is not bool
            or (enable_testnet_order_cancellation and environment != "SANDBOX")
            or type(enable_testnet_cancellation) is not bool
            or (enable_testnet_cancellation and environment != "SANDBOX")
            or type(enable_testnet_protection) is not bool
            or (enable_testnet_protection and environment != "SANDBOX")
            or type(enable_testnet_settings) is not bool
            or (enable_testnet_settings and environment != "SANDBOX")
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
        self._cancel = enable_testnet_cancellation
        self._order_cancel = enable_testnet_order_cancellation
        self._protection = enable_testnet_protection
        self._settings = enable_testnet_settings

    def __call__(self, method, path, params):
        if (
            method == "GET"
            and path in self._READS
            or method == "POST"
            and path == "/fapi/v1/order"
            and self._trading
            or method == "POST"
            and path == "/fapi/v1/algoOrder"
            and self._protection
            or method == "POST"
            and path in {"/fapi/v1/leverage", "/fapi/v1/marginType"}
            and self._settings
            or method == "DELETE"
            and path == "/fapi/v1/algoOrder"
            and self._cancel
            or method == "DELETE"
            and path == "/fapi/v1/order"
            and self._order_cancel
        ):
            pass
        else:
            raise ExchangeTransportError("ENDPOINT_OR_WRITE_DISABLED")
        if method == "POST" and path in {
            "/fapi/v1/leverage",
            "/fapi/v1/marginType",
        }:
            expected = (
                {"symbol", "leverage"}
                if path.endswith("/leverage")
                else {"symbol", "marginType"}
            )
            if (
                not isinstance(params, dict)
                or set(params) != expected
                or not isinstance(params.get("symbol"), str)
                or not re.fullmatch(r"[A-Z0-9_]{1,40}", params["symbol"])
                or (
                    path.endswith("/leverage")
                    and (
                        type(params.get("leverage")) is not int
                        or params["leverage"] not in {1, 2, 3, 4, 5, 8}
                    )
                )
                or (
                    path.endswith("/marginType")
                    and params.get("marginType") not in {"ISOLATED", "CROSSED"}
                )
            ):
                raise ValueError("bounded Testnet symbol settings required")
        if (
            method == "DELETE"
            and path == "/fapi/v1/order"
            and (
                not isinstance(params, dict)
                or set(params) != {"symbol", "origClientOrderId"}
                or not isinstance(params["symbol"], str)
                or not re.fullmatch(r"[A-Z0-9_]{1,40}", params["symbol"])
                or not isinstance(params["origClientOrderId"], str)
                or not re.fullmatch(r"v2[0-9a-f]{32}", params["origClientOrderId"])
            )
        ):
            raise ValueError("scoped V2 cancellation identity required")
        if method == "POST" and path == "/fapi/v1/algoOrder":
            # This capability cannot create an opening order, even if miscalled.
            required = {
                "algoType": "CONDITIONAL",
                "positionSide": "BOTH",
                "closePosition": "true",
                "workingType": "MARK_PRICE",
                "priceProtect": "false",
            }
            if (
                not isinstance(params, dict)
                or set(params)
                != set(required)
                | {"symbol", "side", "type", "triggerPrice", "clientAlgoId"}
                or any(params.get(k) != v for k, v in required.items())
                or params.get("type") not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
                or params.get("side") not in {"BUY", "SELL"}
            ):
                raise ExchangeTransportError("PROTECTION_CLOSE_ALL_REQUIRED")
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
            target = path + "?" + signed if method in {"GET", "DELETE"} else path
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
