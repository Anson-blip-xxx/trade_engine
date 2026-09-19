"""Injected signed-query transport for protection verification."""

import pytest

from position_identity.slot import ExchangePositionKey
from position_protection.binance_query import (
    BinanceQueryErrorCode,
    BinanceVerificationEndpoints,
    BinanceVerificationQueryError,
    InjectedBinanceVerificationQueryAdapter,
)


def _key():
    return ExchangePositionKey.one_way(
        account_principal_id="query-adapter",
        environment="SANDBOX",
        symbol="BTCUSDT",
    )


def _endpoints():
    return BinanceVerificationEndpoints(
        position_risk_path="/approved/positionRisk",
        open_algo_orders_path="/approved/algo/openAlgoOrders",
    )


def test_fetch_uses_two_symbol_scoped_queries_and_completion_clock():
    calls = []

    def signed_get(path, params):
        calls.append((path, params))
        return [{"path": path}]

    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=signed_get,
        clock=lambda: 123.5,
        endpoints=_endpoints(),
    )
    snapshot = adapter.fetch(_key())

    assert calls == [
        ("/approved/positionRisk", {"symbol": "BTCUSDT"}),
        ("/approved/algo/openAlgoOrders", {"symbol": "BTCUSDT"}),
    ]
    assert calls[0][1] is not calls[1][1]
    assert snapshot.position_risk_payload == [{"path": "/approved/positionRisk"}]
    assert snapshot.open_algo_orders_payload == [
        {"path": "/approved/algo/openAlgoOrders"}
    ]
    assert snapshot.observed_at == 123.5


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/approved/positionRisk", BinanceQueryErrorCode.POSITION_QUERY_FAILED),
        ("/approved/algo/openAlgoOrders", BinanceQueryErrorCode.ALGO_QUERY_FAILED),
    ],
)
def test_transport_exceptions_are_typed_by_query(path, code):
    def signed_get(actual, _params):
        if actual == path:
            raise TimeoutError("timeout")
        return []

    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=signed_get, clock=lambda: 1, endpoints=_endpoints()
    )
    with pytest.raises(BinanceVerificationQueryError) as captured:
        adapter.fetch(_key())
    assert captured.value.code is code


def test_transport_exception_details_are_not_exposed():
    def signed_get(_path, _params):
        raise RuntimeError("signature=super-secret&apiKey=also-secret")

    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=signed_get, clock=lambda: 1, endpoints=_endpoints()
    )
    with pytest.raises(BinanceVerificationQueryError) as captured:
        adapter.fetch(_key())
    assert str(captured.value) == "signed query transport failed"
    assert isinstance(captured.value.__cause__, RuntimeError)


@pytest.mark.parametrize("payload", [None, {}, {"code": -1000, "msg": "bad"}])
def test_non_list_business_or_http_payload_is_query_failure(payload):
    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=lambda _path, _params: payload,
        clock=lambda: 1,
        endpoints=_endpoints(),
    )
    with pytest.raises(BinanceVerificationQueryError) as captured:
        adapter.fetch(_key())
    assert captured.value.code is BinanceQueryErrorCode.POSITION_QUERY_FAILED
    assert "signature" not in str(captured.value).lower()


@pytest.mark.parametrize("clock", [lambda: None, lambda: float("nan"), lambda: -1])
def test_invalid_completion_clock_is_typed(clock):
    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=lambda _path, _params: [],
        clock=clock,
        endpoints=_endpoints(),
    )
    with pytest.raises(BinanceVerificationQueryError) as captured:
        adapter.fetch(_key())
    assert captured.value.code is BinanceQueryErrorCode.INVALID_CLOCK


def test_clock_exception_details_are_not_exposed():
    def clock():
        raise RuntimeError("internal clock detail")

    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=lambda _path, _params: [],
        clock=clock,
        endpoints=_endpoints(),
    )
    with pytest.raises(BinanceVerificationQueryError) as captured:
        adapter.fetch(_key())
    assert str(captured.value) == "query completion clock failed"
    assert isinstance(captured.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    "values",
    [
        ("fapi/v2/positionRisk", "/algo"),
        ("/position?symbol=BTC", "/algo"),
        ("/same", "/same"),
        ("/position risk", "/algo"),
        (" /position", "/algo"),
        ("/position ", "/algo"),
    ],
)
def test_endpoints_must_be_explicit_absolute_and_unparameterized(values):
    with pytest.raises(ValueError):
        BinanceVerificationEndpoints(*values)


def test_adapter_contains_no_credentials_or_endpoint_defaults():
    adapter = InjectedBinanceVerificationQueryAdapter(
        signed_get=lambda _path, _params: [],
        clock=lambda: 1,
        endpoints=_endpoints(),
    )
    assert not hasattr(adapter, "api_key")
    assert not hasattr(adapter, "secret")
