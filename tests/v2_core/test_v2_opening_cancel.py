import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest
from test_v2_account_coverage import Account
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent
from test_v2_protection_child import SCOPE
from test_v2_transport import Connection, transport

from v2_core.opening_cancel import TestnetOpeningCancel
from v2_core.opening_halt import halt_opening
from v2_core.partial_open_protection import PartialOpenProtection
from v2_core.protection import ProtectionSpec
from v2_core.service import TradingData
from v2_core.state import BusinessState
from v2_core.transport import ExchangeTransportError

database = database_fixture


@pytest.fixture
def case(database):
    data = TradingData(database)
    original = intent()
    data.accept(original, evidence())
    order_id, client = data.orders.prepare(original.intent_id)
    data.orders.transition(
        order_id, expected_version=1, status="SUBMITTING", evidence={}
    )

    class Venue(Account):
        def __init__(self):
            super().__init__()
            self.deletes, self.posts = [], []
            self.state, self.qty = "PARTIALLY_FILLED", "0.005"
            self.lose, self.accept, self.missing_fills = False, True, False
            self.parent = None

        def __call__(self, method, path, params):
            if path == "/fapi/v1/order":
                assert params == {"symbol": "BTCUSDT", "origClientOrderId": client}
                if method == "DELETE":
                    journal = BusinessState(database).read(worker.cancel.key(order_id))
                    assert json.loads(journal.payload_json)["status"] == "REQUESTED"
                    self.deletes.append(dict(params))
                    if self.accept:
                        self.state = "CANCELED"
                    if self.lose:
                        raise TimeoutError("sensitive-secret")
                    return {"untrusted": "not evidence"}
                assert method == "GET"
                return {
                    "symbol": "BTCUSDT",
                    "clientOrderId": client,
                    "orderId": 10,
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "reduceOnly": False,
                    "origQty": "0.01",
                    "executedQty": self.qty,
                    "status": self.state,
                }
            if path == "/fapi/v1/userTrades":
                assert method == "GET"
                return (
                    []
                    if self.missing_fills or self.qty == "0"
                    else [
                        {
                            "id": 1,
                            "orderId": 10,
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "positionSide": "BOTH",
                            "qty": self.qty,
                            "price": "100",
                            "commission": "0.01",
                            "commissionAsset": "USDT",
                            "time": 1,
                        }
                    ]
                )
            if path == "/fapi/v1/algoOrder":
                if method == "POST":
                    assert self.state in {"CANCELED", "FILLED"}
                    self.posts.append(dict(params))
                    self.parent = {
                        **params,
                        "algoId": 30,
                        "orderType": params["type"],
                        "algoStatus": "NEW",
                        "closePosition": True,
                        "priceProtect": False,
                    }
                assert self.parent is not None
                return dict(self.parent)
            if path == "/fapi/v3/positionRisk":
                self.rows[path] = (
                    []
                    if self.qty == "0"
                    else [
                        {
                            "symbol": "BTCUSDT",
                            "positionSide": "BOTH",
                            "positionAmt": self.qty,
                        }
                    ]
                )
            return super().__call__(method, path, params)

    venue = Venue()
    worker = PartialOpenProtection(
        database,
        venue,
        scope=SCOPE,
        allow_writes=True,
        clock_ms=lambda: 1000,
        reference=lambda _: {
            "symbol": "BTCUSDT",
            "environment": "SANDBOX",
            "mark_price": "100",
            "tick_size": "0.1",
            "observed_at_ms": 1000,
        },
    )
    spec = ProtectionSpec(original.intent_id, "BTCUSDT", "SELL", "STOP_MARKET", "99")
    return data, order_id, venue, worker, spec


def test_partial_cancel_fill_reconcile_stop_and_restart(case):
    _, order, venue, worker, spec = case
    assert worker.ensure(order, spec)["status"] == "NEW"
    assert worker.ensure(order, spec)["status"] == "NEW"
    assert len(venue.deletes) == len(venue.posts) == 1
    assert worker.cancel.snapshot(order)["status"] == "CANCELLED"
    assert (
        worker.stop.audit.facts()["episodes"][0]["remaining"] == "0.005000000000000000"
    )


@pytest.mark.parametrize("accepted", [False, True])
def test_ambiguous_cancel_queries_never_retries(case, accepted):
    _, order, venue, worker, spec = case
    venue.lose, venue.accept = True, accepted
    expected = "NEW" if accepted else "OPENING_NOT_FINAL"
    assert worker.ensure(order, spec)["status"] == expected
    worker.cancel.allow_cancel = False
    assert worker.ensure(order, spec)["status"] == expected
    assert len(venue.deletes) == 1 and len(venue.posts) == int(accepted)
    payload = (
        BusinessState(worker.cancel.connect).read(worker.cancel.key(order)).payload_json
    )
    assert "sensitive-secret" not in payload


def test_missing_trade_evidence_blocks_stop_even_after_cancel(case):
    _, order, venue, worker, spec = case
    venue.missing_fills = True
    assert worker.ensure(order, spec) == {
        "status": "OPENING_NOT_FINAL",
        "opening_status": "UNKNOWN",
    }
    assert not venue.posts
    venue.missing_fills = False
    assert worker.ensure(order, spec)["status"] == "NEW"
    assert len(venue.deletes) == 1


@pytest.mark.parametrize(
    "state,qty,expected",
    [("FILLED", "0.01", "NEW"), ("EXPIRED", "0", "NO_LEDGER_EXPOSURE")],
)
def test_terminal_race_needs_no_cancel(case, state, qty, expected):
    _, order, venue, worker, spec = case
    venue.state, venue.qty = state, qty
    assert worker.ensure(order, spec)["status"] == expected
    assert not venue.deletes


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"kind": "TAKE_PROFIT_MARKET"},
        {"episode": "other"},
    ],
)
def test_wrong_protection_identity_never_cancels(case, change):
    _, order, venue, worker, spec = case
    with pytest.raises(ValueError, match="OWNERSHIP"):
        worker.ensure(order, replace(spec, **change))
    assert not venue.deletes and not venue.posts


def test_disabled_and_foreign_scope_never_write(database, case):
    _, order, venue, _, _ = case
    cancel = TestnetOpeningCancel(database, venue, scope=SCOPE)
    assert cancel.cancel_once(order) == "CANCELLATION_DISABLED"
    venue.account_id = "other"
    cancel = TestnetOpeningCancel(
        database, venue, scope=replace(SCOPE, account_id="other"), allow_cancel=True
    )
    with pytest.raises(ValueError):
        cancel.cancel_once(order)
    assert not venue.deletes and not venue.posts


def test_account_lock_prevents_cancel(database, case):
    _, order, venue, worker, _ = case
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert worker.cancel.cancel_once(order) == "BUSY"
    assert not venue.deletes


def test_failed_registration_never_deletes(case, monkeypatch):
    _, order, venue, worker, _ = case
    original = BusinessState.change

    def fail(self, key, **kwargs):
        result = original(self, key, **kwargs)
        if key.namespace == "opening-cancel-v1":
            raise RuntimeError("commit failed")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(BusinessState, "change", fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            worker.cancel.cancel_once(order)
    assert not venue.deletes
    assert BusinessState(worker.cancel.connect).read(worker.cancel.key(order)) is None


def test_prepared_cancel_is_local_only(database):
    data, venue = TradingData(database), Account()
    cancel = TestnetOpeningCancel(database, venue, scope=SCOPE, allow_cancel=True)
    new = intent()
    data.accept(new, evidence())
    prepared, _ = data.orders.prepare(new.intent_id)
    assert cancel.cancel_once(prepared) == "CANCELLED"
    assert not venue.calls


def test_crash_after_registration_before_send_stays_query_only(case):
    _, order, venue, worker, spec = case
    original = worker.cancel.request

    def crash(*_):
        raise KeyboardInterrupt("process crash")

    worker.cancel.request = crash
    with pytest.raises(KeyboardInterrupt):
        worker.ensure(order, spec)
    worker.cancel.request = original
    assert worker.ensure(order, spec)["status"] == "OPENING_NOT_FINAL"
    assert not venue.deletes and not venue.posts


def test_late_fill_after_cancel_timeout_is_recovered_without_resending(case):
    _, order, venue, worker, spec = case
    venue.accept, venue.lose = False, True
    assert worker.ensure(order, spec)["status"] == "OPENING_NOT_FINAL"
    # Separate immutable second fill is required; never mutate the first trade.
    original = worker.cancel.runner.query
    from dataclasses import replace as updated

    def filled(snapshot):
        observation = original(snapshot)
        second = {
            **observation.fills[0],
            "exchange_fill_id": "2",
            "evidence": {"source": "late-fill-test"},
        }
        return updated(
            observation,
            status="FILLED",
            fills=(*observation.fills, second),
            evidence={"fills_complete": True, "executed_quantity": "0.01"},
        )

    worker.cancel.runner.query = filled
    assert worker.cancel.cancel_once(order) == "FILLED"
    assert len(venue.deletes) == 1
    assert (
        worker.stop.audit.facts()["episodes"][0]["remaining"] == "0.010000000000000000"
    )


def test_cancel_cannot_target_a_closing_order(case):
    data, order, venue, worker, spec = case
    worker.cancel.runner.recover(order)
    closing, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.005", request_key="exit"
    )
    with pytest.raises(ValueError, match="OPENING_ORDER_REQUIRED"):
        worker.cancel.cancel_once(closing)
    assert not venue.deletes


def test_cancel_transport_cannot_be_enabled_for_live():
    from v2_core.transport import BinanceSignedTransport

    with pytest.raises(ValueError):
        BinanceSignedTransport(
            account_id="qa",
            environment="LIVE",
            api_key="dummy",
            api_secret="dummy",
            clock_ms=lambda: 1000,
            permit=lambda *_: True,
            enable_testnet_order_cancellation=True,
        )


def test_partial_recovery_halts_new_openings_but_allows_exit(case):
    data, order, _, worker, spec = case
    assert worker.ensure(order, spec)["status"] == "NEW"
    with pytest.raises(ValueError, match="EPISODE_OPENING_HALTED"):
        data.orders.prepare(
            spec.episode,
            quantity="0.005",
            request_key="refill",
            evidence={"reason": "retry"},
        )
    closing, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.005", request_key="exit"
    )
    assert closing


def test_halt_denies_already_prepared_dispatch_without_exchange(database):
    data, venue = TradingData(database), Account()
    original = intent()
    data.accept(original, evidence())
    order, client = data.orders.prepare(original.intent_id)
    assert halt_opening(database, original.intent_id, scope=SCOPE).code == "APPLIED"
    assert (
        halt_opening(database, original.intent_id, scope=SCOPE).code
        == "ALREADY_APPLIED"
    )
    # Identity replay remains valid but cannot grant a new dispatch permit.
    assert data.orders.prepare(original.intent_id) == (order, client)
    cancel = TestnetOpeningCancel(database, venue, scope=SCOPE)
    cancel.runner.risk_check = lambda _: True
    assert cancel.runner.dispatch(order) == "DENIED"
    assert cancel.snapshot(order)["status"] == "CANCELLED"
    assert not venue.calls


def test_halt_rejects_foreign_account_without_persisting(case):
    _, order, _, worker, spec = case
    with pytest.raises(ValueError, match="bound account"):
        halt_opening(
            worker.cancel.connect,
            spec.episode,
            scope=replace(SCOPE, account_id="other"),
        )
    # No halt means ordinary recovery still works; no cancellation was sent.
    assert worker.cancel.runner.recover(order) == "ACKNOWLEDGED"


def test_two_cancel_workers_only_one_delete(case):
    _, order, venue, worker, _ = case
    reached, release = Event(), Event()
    original = worker.cancel.request

    def slow(*args):
        reached.set()
        assert release.wait(10)
        return original(*args)

    worker.cancel.request = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(worker.cancel.cancel_once, order)
        try:
            assert reached.wait(10)
            assert worker.cancel.cancel_once(order) == "BUSY"
        finally:
            release.set()
        assert first.result(timeout=10) == "CANCELLED"
    assert len(venue.deletes) == 1


def test_order_uuid_spelling_cannot_bypass_cancel_journal(case):
    _, order, venue, worker, _ = case
    venue.accept, venue.lose = False, True
    assert worker.cancel.cancel_once(order) == "ACKNOWLEDGED"
    assert worker.cancel.cancel_once(order.upper()) == "ACKNOWLEDGED"
    assert len(venue.deletes) == 1


def test_episode_uuid_spelling_cannot_bypass_halt(database):
    data, venue = TradingData(database), Account()
    original = intent()
    data.accept(original, evidence())
    order, _ = data.orders.prepare(original.intent_id)
    halt_opening(database, original.intent_id.upper(), scope=SCOPE)
    cancel = TestnetOpeningCancel(database, venue, scope=SCOPE)
    cancel.runner.risk_check = lambda _: True
    assert cancel.runner.dispatch(order) == "DENIED"
    assert not venue.calls


@pytest.mark.parametrize("attempt", range(3))
def test_halt_and_additional_order_race_cannot_dispatch(database, attempt):
    data, venue, barrier = TradingData(database), Account(), Barrier(2)
    original = intent()
    data.accept(original, evidence())
    data.orders.prepare(original.intent_id, quantity="0.005")

    def prepare():
        barrier.wait(timeout=10)
        try:
            return data.orders.prepare(
                original.intent_id,
                quantity="0.005",
                request_key=f"extra-{attempt}",
                evidence={"reason": "grid slice"},
            )[0]
        except ValueError as exc:
            assert str(exc) == "EPISODE_OPENING_HALTED"
            return None

    def halt():
        barrier.wait(timeout=10)
        return halt_opening(database, original.intent_id, scope=SCOPE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending, stop = pool.submit(prepare), pool.submit(halt)
        order = pending.result(timeout=10)
        assert stop.result(timeout=10).code == "APPLIED"
    if order:
        cancel = TestnetOpeningCancel(database, venue, scope=SCOPE)
        cancel.runner.risk_check = lambda _: True
        assert cancel.runner.dispatch(order) == "DENIED"
    assert not venue.calls


def test_ordinary_cancel_transport_is_independent_and_signed():
    conn = Connection()
    params = {"symbol": "BTCUSDT", "origClientOrderId": "v2" + "a" * 32}
    transport(conn, enable_testnet_order_cancellation=True)(
        "DELETE", "/fapi/v1/order", params
    )
    args, kwargs = conn.calls[0]
    assert args[0] == "DELETE" and "signature=" in args[1] and kwargs["body"] is None
    for method, path in [
        ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/allOpenOrders"),
    ]:
        with pytest.raises(ExchangeTransportError):
            transport(Connection(), enable_testnet_order_cancellation=True)(
                method, path, params
            )


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"symbol": "BTCUSDT", "orderId": 10},
        {"symbol": "BTCUSDT", "origClientOrderId": "external"},
    ],
)
def test_transport_rejects_unscoped_cancel_identity(params):
    conn = Connection()
    with pytest.raises(ValueError, match="identity"):
        transport(conn, enable_testnet_order_cancellation=True)(
            "DELETE", "/fapi/v1/order", params
        )
    assert not conn.calls
