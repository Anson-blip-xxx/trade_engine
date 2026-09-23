import hashlib
import hmac
import json

import pytest

from v2_core.binance import BinanceFutures
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError


class Connection:
    status = 200
    raw = b'{"ok":true}'
    failure = None
    closed = False

    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.failure:
            raise self.failure

    def getresponse(self):
        return self

    def read(self, limit):
        return self.raw[:limit]

    def getheader(self, key, default):
        return "60" if key == "Retry-After" else default

    def close(self):
        self.closed = True


def transport(conn, **kwargs):
    def factory(host, timeout):
        assert host == "demo-fapi.binance.com" and timeout == 10
        return conn

    return BinanceSignedTransport(
        account_id="qa",
        environment="SANDBOX",
        api_key="dummy-key",
        api_secret="dummy-secret",
        clock_ms=lambda: 1000,
        permit=lambda *_: True,
        connection_factory=factory,
        **kwargs,
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_signed_bytes_match_sent_parameters_and_caller_is_not_mutated(method):
    conn = Connection()
    params = {"symbol": "BTCUSDT", "origClientOrderId": "v2:abc/123"}
    assert transport(conn, enable_trading=True)(method, "/fapi/v1/order", params) == {
        "ok": True
    }
    args, kwargs = conn.calls[0]
    signed = args[1].split("?", 1)[1] if method == "GET" else kwargs["body"]
    payload, signature = signed.rsplit("&signature=", 1)
    assert (
        signature
        == hmac.new(b"dummy-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    assert "timestamp=1000&recvWindow=5000" in payload
    assert "v2%3Aabc%2F123" in payload
    assert kwargs["headers"]["X-MBX-APIKEY"] == "dummy-key"
    assert params == {"symbol": "BTCUSDT", "origClientOrderId": "v2:abc/123"}
    assert conn.closed


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/fapi/v1/order"),
        ("DELETE", "/fapi/v1/order"),
        ("GET", "https://example.org/"),
        ("GET", "/fapi/v1/order?symbol=BTCUSDT"),
    ],
)
def test_default_transport_disallows_writes_and_unlisted_routes(method, path):
    conn = Connection()
    with pytest.raises(ExchangeTransportError, match="DISABLED"):
        transport(conn)(method, path, {})
    assert conn.calls == []


@pytest.mark.parametrize("field", ["timestamp", "signature", "recvWindow"])
def test_signing_override_rejected(field):
    conn = Connection()
    with pytest.raises(ValueError, match="signing"):
        transport(conn)("GET", "/fapi/v1/order", {field: "bad"})
    assert conn.calls == []


@pytest.mark.parametrize("status", [301, 400, 418, 429, 503])
def test_http_errors_do_not_redirect_retry_or_expose_response(status):
    conn = Connection()
    conn.status = status
    conn.raw = json.dumps({"code": -1007, "msg": "sensitive-original-request"}).encode()
    with pytest.raises(ExchangeTransportError) as caught:
        transport(conn, enable_trading=True)("POST", "/fapi/v1/order", {})
    assert "sensitive" not in str(caught.value)
    assert caught.value.status == status and caught.value.code == -1007
    assert caught.value.retry_after == 60
    assert len(conn.calls) == 1 and conn.closed


def test_timeout_is_redacted_and_not_retried():
    conn = Connection()
    conn.failure = TimeoutError("dummy-secret in request")
    with pytest.raises(ExchangeTransportError, match="UNKNOWN") as caught:
        transport(conn, enable_trading=True)("POST", "/fapi/v1/order", {})
    assert caught.value.__suppress_context__
    assert len(conn.calls) == 1 and conn.closed


def test_testnet_cancel_is_explicit_and_signed_in_query():
    conn = Connection()
    with pytest.raises(ExchangeTransportError, match="DISABLED"):
        transport(conn, enable_trading=True)(
            "DELETE", "/fapi/v1/algoOrder", {"algoId": 123}
        )
    assert conn.calls == []
    client = transport(conn, enable_testnet_cancellation=True)
    client("DELETE", "/fapi/v1/algoOrder", {"algoId": 123})
    args, kwargs = conn.calls[0]
    assert args[0] == "DELETE" and kwargs["body"] is None
    payload, signature = args[1].split("?", 1)[1].rsplit("&signature=", 1)
    assert (
        signature
        == hmac.new(b"dummy-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    for method, path in [
        ("POST", "/fapi/v1/order"),
        ("DELETE", "/fapi/v1/algoOpenOrders"),
        ("DELETE", "/fapi/v1/order"),
    ]:
        with pytest.raises(ExchangeTransportError, match="DISABLED"):
            client(method, path, {})
    assert len(conn.calls) == 1


def test_cancellation_switch_cannot_enable_live_transport():
    with pytest.raises(ValueError):
        BinanceSignedTransport(
            account_id="qa",
            environment="LIVE",
            api_key="dummy",
            api_secret="dummy",
            clock_ms=lambda: 1,
            permit=lambda *_: True,
            enable_testnet_cancellation=True,
        )


def test_testnet_symbol_settings_are_separately_gated_and_signed():
    conn = Connection()
    params = {"symbol": "BTCUSDT", "leverage": 2}
    with pytest.raises(ExchangeTransportError, match="DISABLED"):
        transport(conn)("POST", "/fapi/v1/leverage", params)
    client = transport(conn, enable_testnet_settings=True)
    assert client("POST", "/fapi/v1/leverage", params) == {"ok": True}
    args, kwargs = conn.calls[0]
    assert args[:2] == ("POST", "/fapi/v1/leverage")
    assert "symbol=BTCUSDT&leverage=2" in kwargs["body"]


@pytest.mark.parametrize(
    "path,params",
    [
        ("/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 0}),
        ("/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": 6}),
        ("/fapi/v1/leverage", {"symbol": "BTCUSDT", "leverage": True}),
        ("/fapi/v1/marginType", {"symbol": "BTCUSDT", "marginType": "CROSS"}),
        ("/fapi/v1/marginType", {"symbol": "../BTC", "marginType": "CROSSED"}),
        ("/fapi/v1/marginType", {"symbol": "BTCUSDT"}),
    ],
)
def test_testnet_symbol_settings_reject_unbounded_parameters(path, params):
    conn = Connection()
    with pytest.raises(ValueError, match="bounded"):
        transport(conn, enable_testnet_settings=True)("POST", path, params)
    assert conn.calls == []


def test_symbol_settings_switch_cannot_enable_live_transport():
    with pytest.raises(ValueError):
        BinanceSignedTransport(
            account_id="qa",
            environment="LIVE",
            api_key="dummy",
            api_secret="dummy",
            clock_ms=lambda: 1,
            permit=lambda *_: True,
            enable_testnet_settings=True,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/fapi/v3/account",
        "/fapi/v3/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/multiAssetsMargin",
    ],
)
def test_account_inventory_routes_are_read_only(path):
    conn = Connection()
    client = transport(conn)
    assert client("GET", path, {}) == {"ok": True}
    for method in ("POST", "DELETE", "PUT"):
        with pytest.raises(ExchangeTransportError, match="DISABLED"):
            client(method, path, {})
    assert len(conn.calls) == 1


def test_missing_order_retains_ambiguous_identity():
    conn = Connection()
    conn.status, conn.raw = 400, b'{"code":-2013,"msg":"unknown"}'
    assert transport(conn)("GET", "/fapi/v1/order", {}) == {"code": -2013}


@pytest.mark.parametrize("raw", [b"<html>oops</html>", b"null", b"x" * 2_000_001])
def test_invalid_or_oversized_response_is_rejected(raw):
    conn = Connection()
    conn.raw = raw
    with pytest.raises(ExchangeTransportError):
        transport(conn)("GET", "/fapi/v1/order", {})
    assert conn.closed


def test_adapter_must_match_transport_binding():
    with pytest.raises(ValueError, match="binding"):
        BinanceFutures(
            transport(Connection()), account_id="wrong", environment="SANDBOX"
        )


def test_quota_failure_makes_no_request():
    conn = Connection()
    client = transport(conn)
    client._permit = lambda *_: False
    with pytest.raises(ExchangeTransportError, match="QUOTA"):
        client("GET", "/fapi/v1/order", {})
    assert conn.calls == []
