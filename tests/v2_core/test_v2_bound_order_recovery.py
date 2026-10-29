"""Bound-ID recovery is read-only, identity-strict, and fee-complete."""

import json
from copy import deepcopy

import pytest

from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.transport import ExchangeTransportError


def order(**changes):
    return {
        "exchange": "BINANCE",
        "product": "FUTURES",
        "account_id": "qa",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "leg": "CLOSE",
        "quantity": "1",
        "client_order_id": "v2-recovery-qa",
        "reduce_only": True,
        "order_type": "MARKET",
        "exchange_order_id": "123",
        "status": "ACKNOWLEDGED",
        **changes,
    }


def response(**changes):
    return {
        "clientOrderId": "v2-recovery-qa",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "reduceOnly": True,
        "origQty": "1",
        "executedQty": "1",
        "orderId": 123,
        "status": "FILLED",
        **changes,
    }


def fill(**changes):
    return {
        "id": 17,
        "orderId": 123,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "qty": "1",
        "price": "100",
        "commission": "0.01",
        "commissionAsset": "USDT",
        "time": 100,
        "realizedPnl": "3.4",
        **changes,
    }


def absent():
    return ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2013)


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        assert method == "GET", "recovery must never submit, cancel or replace"
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def adapter(transport, **kwargs):
    return BinanceFutures(transport, account_id="qa", environment="SANDBOX", **kwargs)


@pytest.mark.parametrize("first", [absent(), {"code": -2013}])
def test_missing_client_id_falls_back_once_to_persisted_id_and_verifies_fees(first):
    transport = Transport(first, response(), [fill()])
    result = adapter(transport).query(order())
    assert result.status == "FILLED"
    assert result.exchange_order_id == "123"
    assert result.fills[0]["fee"] == "0.01"
    assert result.fills[0]["evidence"]["venue_realized_pnl"] == "3.4"
    assert result.evidence["fills_complete"] is True
    attempts = result.evidence["recovery_query"]["attempts"]
    assert [(a["method"], a["outcome"]) for a in attempts] == [
        ("CLIENT_ORDER_ID", "ABSENT"),
        ("EXCHANGE_ORDER_ID", "FOUND"),
    ]
    assert attempts[1]["transport"] == {}
    assert transport.calls == [
        (
            "GET",
            "/fapi/v1/order",
            {"symbol": "BTCUSDT", "origClientOrderId": "v2-recovery-qa"},
        ),
        ("GET", "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": 123}),
        (
            "GET",
            "/fapi/v1/userTrades",
            {"symbol": "BTCUSDT", "orderId": 123, "fromId": 0, "limit": 1000},
        ),
    ]


@pytest.mark.parametrize("first", [absent(), {"code": -2013}])
@pytest.mark.parametrize("second", [absent(), {"code": -2013, "msg": "secret"}])
def test_two_absent_lookups_are_typed_unknown_with_redacted_bounded_proof(
    first, second
):
    transport = Transport(first, second)
    result = adapter(transport).query(order())
    assert result.status == "UNKNOWN"
    assert result.exchange_order_id == "123" and result.fills == ()
    assert result.evidence["reason"] == "BOUND_ORDER_LOOKUPS_ABSENT"
    proof = result.evidence["recovery_query"]
    assert proof["bound_exchange_order_id"] is True
    assert len(proof["attempts"]) == 2
    assert {a["outcome"] for a in proof["attempts"]} == {"ABSENT"}
    for attempt, raw in zip(proof["attempts"], (first, second), strict=True):
        assert attempt["transport"] == {
            "category": "EXCHANGE_RESPONSE_ERROR",
            "exchange_code": -2013,
            **({"http_status": 400} if isinstance(raw, Exception) else {}),
        }
    assert "secret" not in json.dumps(result.evidence)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("first", [absent(), {"code": -2013}])
def test_unbound_missing_order_preserves_none_contract_and_has_no_fallback(first):
    transport = Transport(first)
    assert adapter(transport).query(order(exchange_order_id=None)) is None
    assert len(transport.calls) == 1


def test_direct_client_lookup_success_does_not_add_order_id_read():
    transport = Transport(response(), [fill()])
    result = adapter(transport).query(order())
    assert result.status == "FILLED"
    assert "recovery_query" not in result.evidence
    assert len(transport.calls) == 2


def test_direct_client_identity_conflict_does_not_trigger_an_alternate_lookup():
    transport = Transport(response(orderId=124))
    with pytest.raises(ValueError, match="order ID changed"):
        adapter(transport).query(order())
    assert len(transport.calls) == 1


@pytest.mark.parametrize("raw", [None, {}, [], {"code": "-2013"}, {"code": -2013.0}])
def test_malformed_response_is_not_an_explicit_absence_or_fallback_permit(raw):
    transport = Transport(raw)
    with pytest.raises(ValueError):
        adapter(transport).query(order())
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("private detail"),
        ExchangeTransportError("QUOTA_DENIED"),
        ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=503, code=-2013),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=429, code=-1003),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2015),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400.0, code=-2013),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2013.0),
    ],
)
def test_timeout_quota_and_nonexact_absence_never_fallback(failure):
    transport = Transport(failure)
    with pytest.raises(type(failure)) as captured:
        adapter(transport).query(order())
    assert captured.value is failure
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "failure", [TimeoutError("private detail"), ExchangeTransportError("QUOTA_DENIED")]
)
def test_failed_fallback_propagates_without_retry_or_trade_read(failure):
    transport = Transport(absent(), failure)
    with pytest.raises(type(failure)) as captured:
        adapter(transport).query(order())
    assert captured.value is failure
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"clientOrderId": "another"},
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"positionSide": "LONG"},
        {"type": "LIMIT"},
        {"reduceOnly": False},
        {"reduceOnly": "true"},
        {"origQty": "2"},
        {"orderId": 124},
        {"orderId": True},
        {"executedQty": "1.01"},
        {"executedQty": "-0.01"},
    ],
)
def test_fallback_rejects_any_identity_or_quantity_mismatch_before_trades(changes):
    transport = Transport(absent(), response(**changes))
    with pytest.raises(ValueError):
        adapter(transport).query(order())
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "bound_id",
    [True, 123, "", "0", "0123", "-123", "1.23", "123 ", "9" * 20, str(2**63)],
)
def test_invalid_persisted_exchange_id_rejected_before_any_io(bound_id):
    transport = Transport()
    with pytest.raises(ValueError, match="bound exchange"):
        adapter(transport).query(order(exchange_order_id=bound_id))
    assert transport.calls == []


@pytest.mark.parametrize(
    "changes", [{"account_id": "other"}, {"environment": "LIVE"}, {"product": "SPOT"}]
)
def test_fallback_cannot_cross_bound_account_or_environment(changes):
    transport = Transport()
    with pytest.raises(ValueError, match="bound venue account"):
        adapter(transport).query(order(**changes))
    assert transport.calls == []


@pytest.mark.parametrize("trades", [[], [fill(qty="0.4")]])
def test_fallback_filled_response_is_unknown_until_complete_trade_quantity(trades):
    result = adapter(Transport(absent(), response(), trades)).query(order())
    assert result.status == "UNKNOWN"
    assert result.evidence["fills_complete"] is False
    assert len(result.fills) == len(trades)


@pytest.mark.parametrize(
    "changes",
    [
        {"orderId": 124},
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"commission": "NaN"},
        {"commissionAsset": ""},
        {"time": True},
    ],
)
def test_fallback_does_not_skip_per_fill_identity_commission_or_time_checks(changes):
    with pytest.raises(ValueError):
        adapter(Transport(absent(), response(), [fill(**changes)])).query(order())


def test_fallback_requires_complete_bounded_trade_pagination():
    page = [fill(id=i, qty="0.001") for i in range(1000)]
    result = adapter(Transport(absent(), response(), page), max_pages=1).query(order())
    assert result.status == "UNKNOWN" and len(result.fills) == 1000
    transport = Transport(absent(), response(), page, [])
    assert adapter(transport).query(order()).status == "FILLED"
    assert transport.calls[-1][2]["fromId"] == 1000


@pytest.mark.parametrize("price,tif", [("101", "GTC"), ("100", "IOC")])
def test_limit_fallback_requires_identical_original_limit_terms(price, tif):
    current = order(order_type="LIMIT", limit_price="100", time_in_force="GTC")
    raw = response(type="LIMIT", price=price, timeInForce=tif)
    with pytest.raises(ValueError, match="limit terms mismatch"):
        adapter(Transport(absent(), raw)).query(current)


@pytest.mark.parametrize(
    "origin,proof_key",
    [
        ("BINANCE_ALGO_CHILD", "native_child_binding"),
        ("APPROVED_TESTNET_MAINTENANCE", "maintenance_binding"),
        ("BINANCE_EXTERNAL_CLOSE", "external_close_binding"),
    ],
)
def test_exchange_created_child_fallback_keeps_venue_and_local_identity(
    origin, proof_key
):
    binding = {
        "origin": origin,
        "exchange_order_id": "123",
        "venue_client_order_id": "venue-child-qa",
    }
    current = order(request_evidence=binding)
    transport = Transport(absent(), response(clientOrderId="venue-child-qa"), [fill()])
    venue = adapter(transport)
    result = venue.query(current)
    assert result.status == "FILLED"
    assert result.client_order_id == current["client_order_id"]
    assert result.evidence[proof_key] == binding
    assert transport.calls[0][2]["origClientOrderId"] == "venue-child-qa"
    assert transport.calls[1][2] == {"symbol": "BTCUSDT", "orderId": 123}
    with pytest.raises(SubmissionNotSent, match="QUERY_ONLY"):
        venue.submit(current)
    assert len(transport.calls) == 3


def test_child_fallback_must_match_original_venue_client_id_not_local_id():
    binding = {
        "origin": "BINANCE_ALGO_CHILD",
        "exchange_order_id": "123",
        "venue_client_order_id": "venue-child-qa",
    }
    transport = Transport(absent(), response())
    with pytest.raises(ValueError, match="identity mismatch"):
        adapter(transport).query(order(request_evidence=binding))
    assert len(transport.calls) == 2


def test_missing_native_child_keeps_local_identity_and_never_becomes_flat():
    binding = {
        "origin": "BINANCE_ALGO_CHILD",
        "exchange_order_id": "123",
        "venue_client_order_id": "venue-child-qa",
    }
    transport = Transport(absent(), absent())
    result = adapter(transport).query(order(request_evidence=binding))
    assert result.status == "UNKNOWN" and result.fills == ()
    assert result.client_order_id == "v2-recovery-qa"
    assert result.exchange_order_id == "123"
    assert result.evidence["native_child_binding"] == binding
    assert result.evidence["recovery_query"]["bound_exchange_order_id"] is True
    assert len(transport.calls) == 2


def test_durable_market_reject_repair_stays_available_without_bound_id():
    failure = {
        "reason": "submission response unavailable",
        "transport": {
            "category": "EXCHANGE_RESPONSE_ERROR",
            "http_status": 400,
            "exchange_code": -4131,
        },
    }
    transport = Transport(absent())
    result = adapter(transport).query(
        order(exchange_order_id=None, status="UNKNOWN", submission_failure=failure)
    )
    assert result.status == "REJECTED"
    assert result.evidence["reason"] == "DURABLE_MARKET_REJECT_CONFIRMED"
    assert len(transport.calls) == 1


def test_bound_id_prevents_rejection_repair_from_conflicting_submit_failure():
    failure = {
        "reason": "submission response unavailable",
        "transport": {
            "category": "EXCHANGE_RESPONSE_ERROR",
            "http_status": 400,
            "exchange_code": -4131,
        },
    }
    result = adapter(Transport(absent(), absent())).query(
        order(status="UNKNOWN", submission_failure=failure)
    )
    assert result.status == "UNKNOWN"


class SubmissionTransport:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        if (method, path) == ("GET", "/fapi/v1/positionSide/dual"):
            return {"dualSidePosition": False}
        assert (method, path) == ("POST", "/fapi/v1/order")
        return deepcopy(self.raw)


def submitted(raw):
    transport = SubmissionTransport(raw)
    result = adapter(transport).submit(order(exchange_order_id=None))
    assert result.status == "ACKNOWLEDGED"
    assert result.exchange_order_id == "123"
    assert result.fills == ()
    assert result.evidence["fills_complete"] is False
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/fapi/v1/positionSide/dual"),
        ("POST", "/fapi/v1/order"),
    ]
    return result


def test_filled_submission_receipt_preserves_verified_facts_but_never_books_fills():
    result = submitted(
        response(
            origQty="1.000000000000000000",
            executedQty="1.000000000000000000",
            avgPrice="100.230000000000000000",
            time=1792500000000,
            updateTime=1792500000001,
        )
    )
    assert result.evidence == {
        "source": "binance-submit",
        "venue_status": "FILLED",
        "fills_complete": False,
        "submission_receipt": {
            "schema": "binance-submit-receipt-v1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "position_side": "BOTH",
            "order_type": "MARKET",
            "reduce_only": True,
            "original_quantity": "1",
            "executed_quantity": "1",
            "average_price": {"status": "OBSERVED", "value": "100.23"},
            "venue_time_ms": {"status": "OBSERVED", "value": 1792500000000},
            "venue_update_time_ms": {"status": "OBSERVED", "value": 1792500000001},
        },
    }


@pytest.mark.parametrize(
    "status,executed", [("NEW", "0"), ("PARTIALLY_FILLED", "0.4"), ("FILLED", "1")]
)
def test_submission_missing_optional_receipt_fields_still_acknowledges(
    status, executed
):
    result = submitted(response(status=status, executedQty=executed))
    receipt = result.evidence["submission_receipt"]
    assert result.evidence["venue_status"] == status
    assert receipt["executed_quantity"] == executed
    for key in ("average_price", "venue_time_ms", "venue_update_time_ms"):
        assert receipt[key] == {"status": "MISSING"}


@pytest.mark.parametrize(
    "value",
    [True, None, 100.0, "", "NaN", "Infinity", "-1", "0", "1e20", "1e-19", {}, []],
)
def test_bad_optional_average_price_never_rejects_or_echoes_response(value):
    result = submitted(response(avgPrice=value))
    assert result.evidence["submission_receipt"]["average_price"] == {
        "status": "UNAVAILABLE"
    }


@pytest.mark.parametrize(
    "field,key", [("time", "venue_time_ms"), ("updateTime", "venue_update_time_ms")]
)
@pytest.mark.parametrize(
    "value", [True, False, None, -1, 253402300800000, 123.0, "123", {}, []]
)
def test_bad_optional_receipt_time_never_rejects_or_echoes_response(field, key, value):
    result = submitted(response(**{field: value}))
    assert result.evidence["submission_receipt"][key] == {"status": "UNAVAILABLE"}


@pytest.mark.parametrize("value", [0, 253402300799999])
def test_receipt_time_boundary_is_observed_not_a_freshness_assertion(value):
    result = submitted(response(time=value, updateTime=value))
    for field in ("venue_time_ms", "venue_update_time_ms"):
        assert result.evidence["submission_receipt"][field] == {
            "status": "OBSERVED",
            "value": value,
        }


@pytest.mark.parametrize("status", ["secret response text", {"secret": "value"}, None])
def test_submission_receipt_copies_only_whitelisted_response_fields(status):
    result = submitted(
        response(
            status=status,
            avgPrice="secret price",
            time="secret time",
            updateTime="secret update",
            apiKey="secret API key",
            secret="secret API secret",
            msg="secret response message",
            url="https://example.invalid/secret",
            fills=[{"secret": "not authoritative"}],
        )
    )
    encoded = json.dumps(result.evidence)
    assert "secret" not in encoded
    assert "https" not in encoded
    assert "apiKey" not in encoded
    assert "clientOrderId" not in encoded and "orderId" not in encoded
    assert result.evidence["venue_status"] == "UNAVAILABLE"
    assert set(result.evidence) == {
        "source",
        "venue_status",
        "fills_complete",
        "submission_receipt",
    }


def test_submission_receipt_does_not_relax_verified_order_identity():
    transport = SubmissionTransport(response(side="BUY", avgPrice="secret optional"))
    with pytest.raises(ValueError, match="identity mismatch"):
        adapter(transport).submit(order(exchange_order_id=None))
    assert len(transport.calls) == 2
