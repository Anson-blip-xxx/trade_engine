"""Real-PG runner callback boundaries; no venue network or actual orders."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_v2_account_risk import SCOPE, configured, prepare, runner

from tests.support.v2_admission import database as database_fixture
from v2_core.binance import BinanceFutures
from v2_core.transport import ExchangeTransportError

database = database_fixture


class Venue:
    def __init__(self):
        self.calls = []
        self.raw = None
        self.after_post = None

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "POST"]

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        if path == "/fapi/v1/positionSide/dual":
            assert method == "GET"
            return {"dualSidePosition": False}
        if path == "/fapi/v1/userTrades":
            assert method == "GET"
            return []
        assert path == "/fapi/v1/order"
        if method == "POST":
            assert params["reduceOnly"] == "true"
            self.raw = {
                "clientOrderId": params["newClientOrderId"],
                "symbol": params["symbol"],
                "side": params["side"],
                "positionSide": "BOTH",
                "type": "MARKET",
                "reduceOnly": True,
                "origQty": params["quantity"],
                "executedQty": "0",
                "orderId": 600,
                "status": "NEW",
            }
            if self.after_post is not None:
                raise self.after_post
        else:
            assert method == "GET"
        return deepcopy(self.raw) if self.raw else {"code": -2013}


@pytest.fixture
def closing(database):
    configured(database)
    intent, data, opening, _ = prepare(database)
    execution = runner(database)
    assert execution.dispatch(opening) == "ACKNOWLEDGED"
    data.ledger.record_fill(
        order_id=opening,
        exchange_fill_id="qa-open-fill",
        quantity="0.01",
        price="100",
        fee="0.0004",
        fee_currency="USDT",
        occurred_at_ms=100,
        evidence={"source": "qa authoritative fill"},
    )
    assert data.orders.transition(
        opening,
        expected_version=3,
        status="FILLED",
        evidence={"fills_complete": True},
    )
    close, _ = data.orders.prepare(
        intent.intent_id,
        leg="CLOSE",
        quantity="0.01",
        request_key="qa-isolated-reduction",
        evidence={"source": "qa"},
    )
    venue = Venue()
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    execution.submit, execution.query = adapter.submit, adapter.query
    return SimpleNamespace(execution=execution, order=close, venue=venue)


@pytest.mark.parametrize("callback", [False, 1, "not-a-function", {}])
def test_noncallable_callback_rejected_before_state_change_or_network(
    closing, callback
):
    before = closing.execution.snapshot(closing.order)
    with pytest.raises(TypeError, match="callable reduction"):
        closing.execution.dispatch(closing.order, reduce_only_check=callback)
    assert closing.execution.snapshot(closing.order) == before
    assert closing.venue.calls == []


def test_open_order_cannot_receive_reduce_only_revalidation_capability(database):
    configured(database)
    _, _, oid, _ = prepare(database)
    execution = runner(database, submit=lambda _: pytest.fail("OPEN must not submit"))
    before = execution.snapshot(oid)
    with pytest.raises(ValueError, match="REDUCTION_REVALIDATION_CLOSE_ONLY"):
        execution.dispatch(
            oid,
            reduce_only_check=lambda _: pytest.fail(
                "OPEN must not call reduction check"
            ),
        )
    assert execution.snapshot(oid) == before


@pytest.mark.parametrize("failure", [False, None, 1, {"allowed": True}, "raise"])
def test_failed_revalidation_is_durable_not_sent_rejection(database, closing, failure):
    calls = []

    def check(request):
        current = closing.execution.snapshot(closing.order)
        assert current["status"] == "SUBMITTING" and current["version"] == 2
        assert request["leg"] == "CLOSE" and request["reduce_only"] is True
        calls.append(request["order_id"])
        if failure == "raise":
            raise RuntimeError("secret callback URL credentials")
        return failure

    assert (
        closing.execution.dispatch(closing.order, reduce_only_check=check) == "REJECTED"
    )
    assert calls == [closing.order]
    assert closing.venue.calls == []
    assert closing.execution.snapshot(closing.order)["status"] == "REJECTED"
    with database() as conn:
        evidence = conn.execute(
            "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='REJECTED'",
            (closing.order,),
        ).fetchone()[0]
    assert evidence == {
        "reason": "REDUCE_ONLY_REVALIDATION_FAILED",
        "submission_sent": False,
    }
    assert "secret" not in json.dumps(evidence)


def test_successful_check_precedes_single_post_and_cannot_mutate_order_request(closing):
    calls = []

    def check(request):
        assert closing.venue.writes == []
        assert closing.execution.snapshot(closing.order)["status"] == "SUBMITTING"
        calls.append(request["order_id"])
        # The callback sees a copy, not a mutable submission capability.
        request["quantity"] = "999"
        request["reduce_only"] = False
        return True

    assert (
        closing.execution.dispatch(closing.order, reduce_only_check=check)
        == "ACKNOWLEDGED"
    )
    # Runner checks before invoking submit; the real adapter checks again after
    # its mode GET. Both must receive an independent immutable-order copy.
    assert calls == [closing.order, closing.order]
    assert len(closing.venue.writes) == 1
    params = closing.venue.writes[0][2]
    assert params["quantity"] == "0.01" and params["reduceOnly"] == "true"


@pytest.mark.parametrize("status", ["ACKNOWLEDGED", "UNKNOWN"])
def test_existing_ambiguous_order_is_query_only_without_callback_or_repost(
    closing, status
):
    if status == "UNKNOWN":
        closing.venue.after_post = TimeoutError(
            "simulated accepted write and lost response"
        )
    assert (
        closing.execution.dispatch(closing.order, reduce_only_check=lambda _: True)
        == status
    )
    assert len(closing.venue.writes) == 1
    closing.venue.after_post = None
    assert (
        closing.execution.dispatch(
            closing.order,
            reduce_only_check=lambda _: pytest.fail(
                "recovery must not revalidate or resend"
            ),
        )
        == "ACKNOWLEDGED"
    )
    assert len(closing.venue.writes) == 1
    assert any(path == "/fapi/v1/userTrades" for _, path, _ in closing.venue.calls)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("secret post response"),
        ExchangeTransportError("NETWORK_OUTCOME_UNKNOWN"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=503, code=-1007),
    ],
)
def test_failure_after_post_remains_unknown_not_safely_rejected(
    database, closing, failure
):
    closing.venue.after_post = failure
    assert (
        closing.execution.dispatch(closing.order, reduce_only_check=lambda _: True)
        == "UNKNOWN"
    )
    assert len(closing.venue.writes) == 1
    current = closing.execution.snapshot(closing.order)
    assert current["status"] == "UNKNOWN"
    with database() as conn:
        events = conn.execute(
            "SELECT status,evidence FROM v2_order_events WHERE order_id=%s ORDER BY version",
            (closing.order,),
        ).fetchall()
    assert not any(status == "REJECTED" for status, _ in events)
    assert events[-1][1]["reason"] == "submission response unavailable"
    assert events[-1][1].get("submission_sent") is not False
    assert "secret" not in json.dumps(events)
