from dataclasses import asdict, replace

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent

from v2_core.account_risk import AccountScope
from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.protection_child import ProtectionChildReconciler
from v2_core.runner import ExecutionRunner
from v2_core.service import TradingData
from v2_core.state import BusinessState, StateKey

database = database_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


class Venue:
    account_id = SCOPE.account_id
    environment = "SANDBOX"

    def __init__(self, spec):
        self.calls = []
        self.parent = {
            **TestnetProtection(None, self, scope=SCOPE).params(spec),
            "orderType": spec.kind,
            "closePosition": True,
            "priceProtect": False,
            "algoId": 99,
            "algoStatus": "FINISHED",
            "actualOrderId": "555",
            "actualQty": "0.01",
        }
        self.child = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "type": "MARKET",
            "reduceOnly": True,
            "orderId": 555,
            "clientOrderId": "exchange-generated",
            "origQty": "0.01",
            "executedQty": "0.01",
            "status": "FILLED",
        }
        self.trades = [
            {
                "id": 2,
                "orderId": 555,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "qty": "0.01",
                "price": "101",
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": 3,
                "realizedPnl": "0.01",
            }
        ]

    def __call__(self, method, path, params):
        assert method == "GET"
        self.calls.append((path, params))
        if path.endswith("algoOrder"):
            return dict(self.parent)
        if path.endswith("/order"):
            assert (
                params.get("orderId") == 555
                or params.get("origClientOrderId") == "exchange-generated"
            )
            return dict(self.child)
        if path.endswith("userTrades"):
            return [dict(t) for t in self.trades]
        raise AssertionError(path)


@pytest.fixture
def setup(database):
    original = intent()
    data = TradingData(database)
    data.accept(original, evidence())
    order, _ = data.orders.prepare(original.intent_id)
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="1",
        quantity="0.01",
        price="100",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=1,
        evidence={"source": "test"},
    )
    data.orders.transition(
        order,
        expected_version=2,
        status="FILLED",
        exchange_order_id="111",
        evidence={"fills_complete": True},
    )
    spec = ProtectionSpec(original.intent_id, "BTCUSDT", "SELL", "STOP_MARKET", "99")
    venue = Venue(spec)
    protection = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    protection.save(spec, None, {"spec": asdict(spec), "status": "SENDING"})
    return data, spec, venue, ProtectionChildReconciler(database, venue, scope=SCOPE)


def test_adoption_is_atomic_deduplicated_and_never_submits(database, setup):
    data, spec, venue, worker = setup
    first = worker.reconcile(spec)
    assert first["status"] == "FILLED"
    assert worker.reconcile(spec) == first
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    close = next(o for o in trace["orders"] if o["leg"] == "CLOSE")
    assert close["client_order_id"] != "exchange-generated"
    assert close["request_evidence"]["venue_client_order_id"] == "exchange-generated"
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runner = ExecutionRunner(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: False,
        scope=SCOPE,
    )
    snapshot = runner.snapshot(first["order_id"])
    assert adapter.query(snapshot).client_order_id == snapshot["client_order_id"]
    calls = len(venue.calls)
    with pytest.raises(SubmissionNotSent):
        adapter.submit(snapshot)
    assert len(venue.calls) == calls


def test_partial_child_continues_through_normal_query_recovery(database, setup):
    data, spec, venue, worker = setup
    venue.child.update(status="PARTIALLY_FILLED", executedQty="0.005")
    venue.trades[0]["qty"] = "0.005"
    result = worker.reconcile(spec)
    assert result["status"] == "ACKNOWLEDGED"
    venue.child.update(status="FILLED", executedQty="0.01")
    venue.trades.append({**venue.trades[0], "id": 3})
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runner = ExecutionRunner(
        database,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: False,
        scope=SCOPE,
    )
    assert runner.recover(result["order_id"]) == "FILLED"
    assert len(data.trace(spec.episode)["fills"]) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"reduceOnly": False},
        {"side": "BUY"},
        {"symbol": "ETHUSDT"},
        {"orderId": 666},
        {"origQty": "0.02"},
        {"type": "LIMIT"},
    ],
)
def test_wrong_child_fails_before_any_local_close(setup, change):
    data, spec, venue, worker = setup
    venue.child.update(change)
    with pytest.raises(ValueError):
        worker.reconcile(spec)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_trigger_is_not_fill_and_absent_child_is_pending(setup):
    data, spec, venue, worker = setup
    venue.parent.update(algoStatus="NEW")
    assert worker.reconcile(spec)["status"] == "WAITING_TRIGGER"
    venue.parent.update(algoStatus="TRIGGERED", actualOrderId="")
    assert worker.reconcile(spec)["status"] == "WAITING_CHILD_ID"
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_incomplete_trades_do_not_claim_filled(setup):
    data, spec, venue, worker = setup
    venue.trades = []
    assert worker.reconcile(spec)["status"] == "UNKNOWN"
    assert len(data.trace(spec.episode)["fills"]) == 1


def test_existing_close_reservation_blocks_conflicting_native_close(setup):
    data, spec, _venue, worker = setup
    data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="manual"
    )
    with pytest.raises(ValueError, match="close quantity"):
        worker.reconcile(spec)
    assert len(data.trace(spec.episode)["orders"]) == 2


def test_wrong_episode_side_rejected_before_venue(setup):
    _data, spec, venue, worker = setup
    with pytest.raises(ValueError, match="SCOPE"):
        worker.reconcile(replace(spec, side="BUY"))
    assert venue.calls == []


def test_existing_global_child_owner_cannot_be_reassigned(database, setup):
    data, spec, _venue, worker = setup
    key = StateKey(
        **asdict(SCOPE), namespace="native-child-owner-v1", key="BTCUSDT:555"
    )
    BusinessState(database).change(
        key,
        expected_version=0,
        request_key="bind",
        payload={"episode": "another", "binding": {}},
        reason="test",
    )
    with pytest.raises(ValueError, match="ALREADY_OWNED"):
        worker.reconcile(spec)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_canceled_partial_native_child_exit_only_closes_remaining(
    database, setup, monkeypatch
):
    from types import SimpleNamespace

    from services import v2_testnet_roundtrip as rt
    from v2_core.runner import ExchangeObservation

    data, spec, venue, worker = setup
    venue.child.update(status="EXPIRED", executedQty="0.005")
    venue.trades[0]["qty"] = "0.005"
    assert worker.reconcile(spec)["status"] == "CANCELLED"
    sent = []

    def submit(order):
        assert (
            order["quantity"] == "0.005000000000000000" and order["reduce_only"] is True
        )
        sent.append(order)
        return ExchangeObservation(
            order["client_order_id"],
            "FILLED",
            "556",
            (
                {
                    "exchange_fill_id": "3",
                    "quantity": "0.005",
                    "price": "101",
                    "fee": "0.01",
                    "fee_currency": "USDT",
                    "occurred_at_ms": 4,
                    "evidence": {"source": "test"},
                },
            ),
            {"fills_complete": True},
        )

    runner = ExecutionRunner(
        database,
        submit=submit,
        query=lambda _: None,
        risk_check=lambda _: True,
        scope=SCOPE,
    )
    monkeypatch.setattr(rt, "EPISODE", spec.episode)
    rt.close_owned(
        SimpleNamespace(data=data, execution=runner),
        lambda *a: [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.005"}
        ],
    )
    assert len(sent) == 1
    assert (
        data.ledger.report(spec.episode, settlement_currency="USDT")["opened_quantity"]
        == data.ledger.report(spec.episode, settlement_currency="USDT")[
            "closed_quantity"
        ]
    )
