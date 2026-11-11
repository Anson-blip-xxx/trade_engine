"""Verify proof expiry at adapter, ownership-lock and signed-I/O boundaries."""

import json
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from test_v2_symbol_exit_isolation import assert_no_new_orders, build_case

from tests.support.v2_admission import database as database_fixture
from v2_core.binance import BinanceFutures
from v2_core.guarded_transport import GuardedSignedRequest
from v2_core.transport import BinanceSignedTransport

database = database_fixture


def bind(case, request):
    adapter = BinanceFutures(
        request,
        account_id=case.stage.scope.account_id,
        environment=case.stage.scope.environment,
    )
    case.runtime.execution.submit, case.runtime.execution.query = (
        adapter.submit,
        adapter.query,
    )


def rejected(case):
    result = case.stage.run_once()
    assert_no_new_orders(case, result)
    close = result["exits"][case.btc["intent_id"]]
    assert close["outcome"] == "REJECTED", result
    assert case.runtime.execution.snapshot(close["order_id"])["status"] == "REJECTED"


def test_whole_inventory_age_cannot_be_replaced_by_finish_age(database, monkeypatch):
    case = build_case(database)
    original = case.stage.audit.inventory.collect

    def collected(*args, **kwargs):
        result = original(*args, **kwargs)
        return {**result, "started_at_ms": result["finished_at_ms"] - 5000}

    def risk_check(_):
        case.clock[0] += 5000
        return True

    monkeypatch.setattr(case.stage.audit.inventory, "collect", collected)
    case.runtime.risk_check = risk_check
    rejected(case)


@pytest.mark.parametrize("change", ["expiry", "triggered_stop"])
def test_adapter_mode_read_cannot_outlive_verified_reduction(database, change):
    case = build_case(database)

    class SlowMode:
        account_id = case.stage.scope.account_id
        environment = case.stage.scope.environment

        def __call__(self, method, path, params):
            value = case.venue(method, path, params)
            if method == "GET" and path == "/fapi/v1/positionSide/dual":
                if change == "expiry":
                    case.clock[0] += 10000
                else:
                    case.venue.parent["algoStatus"] = "TRIGGERING"
                    case.venue.rows["/fapi/v1/openAlgoOrders"][0]["algoStatus"] = (
                        "TRIGGERING"
                    )
            return value

    bind(case, SlowMode())
    rejected(case)


@pytest.mark.parametrize("change", ["expiry", "triggered_stop"])
def test_guard_wait_is_followed_by_fresh_reduction_revalidation(database, change):
    case = build_case(database)
    phases = []

    @contextmanager
    def delayed_guard(*, operation):
        assert operation == "MANAGE"
        phases.append("acquired")
        if change == "expiry":
            case.clock[0] += 10000
        else:
            case.venue.parent["algoStatus"] = "TRIGGERING"
            case.venue.rows["/fapi/v1/openAlgoOrders"][0]["algoStatus"] = "TRIGGERING"
        try:
            yield
        finally:
            phases.append("released")

    bind(
        case,
        GuardedSignedRequest(
            case.venue,
            account_id=case.stage.scope.account_id,
            environment=case.stage.scope.environment,
            guard=delayed_guard,
        ),
    )
    rejected(case)
    assert phases == ["acquired", "released"]


@pytest.mark.parametrize("expire", [False, True])
def test_signed_transport_checks_after_quota_before_any_post_bytes(database, expire):
    case = build_case(database)
    wire_calls, guards = [], []

    class Wire:
        status = 200

        def request(self, method, target, *, body, headers):
            path = urlsplit(target).path
            values = parse_qs(body if method == "POST" else urlsplit(target).query)
            params = {
                key: value[0]
                for key, value in values.items()
                if key not in {"timestamp", "signature", "recvWindow"}
            }
            wire_calls.append((method, path))
            self.payload = case.venue(method, path, params)

        def getresponse(self):
            return self

        def read(self, _):
            return json.dumps(self.payload).encode()

        def getheader(self, _, default):
            return default

        def close(self):
            pass

    def permit(method, _):
        if expire and method == "POST":
            case.clock[0] += 10000
        return True

    @contextmanager
    def guard(*, operation):
        guards.append(operation)
        yield

    signed = BinanceSignedTransport(
        account_id=case.stage.scope.account_id,
        environment=case.stage.scope.environment,
        api_key="qa-key",
        api_secret="qa-secret",
        clock_ms=lambda: case.clock[0],
        permit=permit,
        enable_trading=True,
        connection_factory=lambda *args, **kwargs: Wire(),
    )
    bind(
        case,
        GuardedSignedRequest(
            signed,
            account_id=case.stage.scope.account_id,
            environment=case.stage.scope.environment,
            guard=guard,
        ),
    )
    if expire:
        rejected(case)
        assert not any(method == "POST" for method, _ in wire_calls)
    else:
        result = case.stage.run_once()
        assert result["status"] == "BLOCKED"
        close = result["exits"][case.btc["intent_id"]]
        assert close["status"] == "DISPATCHED", result
        assert close["outcome"] == "ACKNOWLEDGED"
        assert sum(method == "POST" for method, _ in wire_calls) == 1
        assert len(case.venue.order_writes) == 1
    assert guards == ["MANAGE"]
