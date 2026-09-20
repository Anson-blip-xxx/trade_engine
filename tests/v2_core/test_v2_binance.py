from copy import deepcopy

import pytest

from v2_core.binance import BinanceFutures


def order():
    return {
        "exchange": "BINANCE",
        "product": "FUTURES",
        "account_id": "qa",
        "environment": "SANDBOX",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "quantity": "1",
        "client_order_id": "v2test",
        "reduce_only": False,
        "order_type": "MARKET",
        "exchange_order_id": None,
    }


def response(**changes):
    return dict(
        clientOrderId="v2test",
        symbol="BTCUSDT",
        side="BUY",
        positionSide="BOTH",
        type="MARKET",
        reduceOnly=False,
        origQty="1",
        executedQty="1",
        orderId=123,
        status="FILLED",
        **changes,
    )


def fill(identity=1, quantity="1"):
    return {
        "id": identity,
        "orderId": 123,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "BOTH",
        "qty": quantity,
        "price": "100",
        "commission": "0.01",
        "commissionAsset": "USDT",
        "time": 100,
        "realizedPnl": "0",
    }


class Transport:
    def __init__(self, raw=None, pages=None):
        self.raw = response() if raw is None else raw
        self.pages = [[fill()]] if pages is None else pages
        self.calls = []
        self.mode = False

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        if path.endswith("/dual"):
            return {"dualSidePosition": self.mode}
        if path.endswith("/userTrades"):
            return self.pages.pop(0)
        return deepcopy(self.raw)


def adapter(transport, **kw):
    return BinanceFutures(transport, account_id="qa", environment="SANDBOX", **kw)


def test_submit_once_then_reconcile_commissions_before_finality():
    transport = Transport()
    venue = adapter(transport)
    assert venue.submit(order()).status == "ACKNOWLEDGED"
    result = venue.query(order())
    assert result.status == "FILLED"
    assert result.fills[0]["fee"] == "0.01"
    assert result.evidence["fills_complete"] is True
    assert len([c for c in transport.calls if c[0] == "POST"]) == 1
    assert transport.calls[1][2]["newClientOrderId"] == "v2test"


def test_notfound_is_not_permission_to_resubmit():
    transport = Transport(raw={"code": -2013})
    assert adapter(transport).query(order()) is None
    assert all(c[0] == "GET" for c in transport.calls)


@pytest.mark.parametrize(
    "changes", [{"account_id": "other"}, {"environment": "LIVE"}, {"product": "SPOT"}]
)
def test_scope_rejected_before_network(changes):
    transport = Transport()
    with pytest.raises(ValueError, match="bound"):
        adapter(transport).submit({**order(), **changes})
    assert transport.calls == []


@pytest.mark.parametrize("mode", [True, "false", None, 0])
def test_hedge_or_ambiguous_mode_never_submits(mode):
    transport = Transport()
    transport.mode = mode
    with pytest.raises(ValueError, match="one-way"):
        adapter(transport).submit(order())
    assert all(c[0] == "GET" for c in transport.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETHUSDT"},
        {"side": "SELL"},
        {"origQty": "2"},
        {"reduceOnly": True},
        {"positionSide": "LONG"},
        {"orderId": True},
    ],
)
def test_bad_order_identity_rejected_before_fills(changes):
    transport = Transport(raw={**response(), **changes})
    with pytest.raises(ValueError):
        adapter(transport).query(order())
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        ("FILLED", "FILLED"),
        ("CANCELED", "CANCELLED"),
        ("EXPIRED", "CANCELLED"),
        ("PARTIALLY_FILLED", "ACKNOWLEDGED"),
        ("OTHER", "UNKNOWN"),
    ],
)
def test_terminal_status_requires_complete_fills(status, expected):
    raw = {**response(), "status": status}
    assert adapter(Transport(raw=raw)).query(order()).status == expected
    assert adapter(Transport(raw=raw, pages=[[]])).query(order()).status == "UNKNOWN"


def test_pagination_cannot_claim_complete_at_page_budget():
    page = [fill(i, "0.001") for i in range(1000)]
    result = adapter(Transport(pages=[page]), max_pages=1).query(order())
    assert result.status == "UNKNOWN" and len(result.fills) == 1000
    transport = Transport(pages=[page, []])
    assert adapter(transport).query(order()).status == "FILLED"
    assert transport.calls[-1][2]["fromId"] == 1000


@pytest.mark.parametrize(
    "pages",
    [
        [[fill(), fill()]],
        [[{**fill(), "orderId": 999}]],
        [[{**fill(), "commission": "NaN"}]],
        [[{**fill(), "time": True}]],
    ],
)
def test_malformed_trade_group_rejected(pages):
    with pytest.raises(ValueError):
        adapter(Transport(pages=pages)).query(order())


def test_limit_close_preserves_price_tif_and_reduce_only():
    closing = {
        **order(),
        "order_type": "LIMIT",
        "limit_price": "105",
        "time_in_force": "GTC",
        "side": "SELL",
        "reduce_only": True,
    }
    raw = {
        **response(),
        "type": "LIMIT",
        "price": "105.0",
        "timeInForce": "GTC",
        "side": "SELL",
        "reduceOnly": True,
    }
    transport = Transport(raw=raw)
    assert adapter(transport).submit(closing).status == "ACKNOWLEDGED"
    sent = transport.calls[-1][2]
    assert (sent["price"], sent["timeInForce"], sent["reduceOnly"]) == (
        "105",
        "GTC",
        "true",
    )


def test_timeout_does_not_retry_write():
    calls = []

    def request(method, path, params):
        calls.append(method)
        if method == "GET":
            return {"dualSidePosition": False}
        raise TimeoutError("simulated lost acknowledgement")

    with pytest.raises(TimeoutError):
        adapter(request).submit(order())
    assert calls == ["GET", "POST"]


def test_read_preflight_failure_is_definitely_not_submitted():
    from v2_core.errors import SubmissionNotSent

    calls = []

    def request(method, path, params):
        calls.append(method)
        raise TimeoutError("private detail")

    with pytest.raises(SubmissionNotSent, match="PREFLIGHT"):
        adapter(request).submit(order())
    assert calls == ["GET"]
