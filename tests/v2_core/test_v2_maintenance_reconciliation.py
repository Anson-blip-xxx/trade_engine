from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_protection_child import SCOPE
from test_v2_protection_child import setup as setup_fixture

from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.maintenance_reconciliation import MaintenanceCloseReconciler
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState, StateKey

database = database_fixture
setup = setup_fixture


@pytest.fixture
def maintenance(database, setup):
    data, spec, venue, _ = setup
    case, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.003", request_key="unknown-exit"
    )
    data.orders.transition(case, expected_version=1, status="SUBMITTING", evidence={})
    data.orders.transition(
        case,
        expected_version=2,
        status="UNKNOWN",
        evidence={"reason": "original uncertainty"},
    )
    params = {
        "symbol": spec.symbol,
        "side": "SELL",
        "positionSide": "BOTH",
        "type": "MARKET",
        "reduceOnly": "true",
        "quantity": "0.01",
        "newClientOrderId": "exchange-generated",
    }
    action = {"key": "close", "kind": "CLOSE", "params": params}
    store = BusinessState(database)
    for suffix, payload in (
        ("plan", {"symbol": spec.symbol, "actions": [action]}),
        (
            "close",
            {
                "status": "CONFIRMED",
                "action": action,
                "proof": {"order": dict(venue.child), "fills": venue.trades},
            },
        ),
    ):
        store.change(
            StateKey(
                **asdict(SCOPE),
                namespace="testnet-scoped-cleanup-v1",
                key=case + ":" + suffix,
            ),
            expected_version=0,
            request_key="fixture",
            payload=payload,
            reason="QA_CONFIRMED_MAINTENANCE",
        )
    return data, spec, venue, case


def test_import_is_idempotent_preserves_unknown_and_never_posts(database, maintenance):
    data, spec, venue, case = maintenance
    worker = MaintenanceCloseReconciler(database, venue, scope=SCOPE)
    first = worker.reconcile(spec.episode, case)
    assert worker.reconcile(spec.episode, case) == first
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 3 and len(trace["fills"]) == 2
    assert (
        next(o for o in trace["orders"] if o["order_id"] == case)["status"] == "UNKNOWN"
    )
    assert first["historical_unknown_resolved"] is False
    adapter = BinanceFutures(
        venue, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    runner = ExecutionRunner(
        database, submit=adapter.submit, query=adapter.query, risk_check=lambda _: False
    )
    snapshot = runner.snapshot(first["order_id"])
    assert snapshot["status"] == "FILLED"
    assert adapter.query(snapshot).client_order_id == snapshot["client_order_id"]
    with pytest.raises(SubmissionNotSent):
        adapter.submit(snapshot)


def test_concurrent_imports_create_one_close_and_fill(database, maintenance):
    data, spec, venue, case = maintenance

    def adopt(_):
        return MaintenanceCloseReconciler(database, venue, scope=SCOPE).reconcile(
            spec.episode, case
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(adopt, range(4)))
    assert all(r == results[0] for r in results)
    assert len(data.trace(spec.episode)["fills"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "ETHUSDT"),
        ("side", "BUY"),
        ("origQty", "0.02"),
        ("reduceOnly", False),
        ("orderId", 556),
        ("status", "PARTIALLY_FILLED"),
    ],
)
def test_changed_venue_order_does_not_partially_import(
    database, maintenance, field, value
):
    data, spec, venue, case = maintenance
    venue.child[field] = value
    with pytest.raises(ValueError):
        MaintenanceCloseReconciler(database, venue, scope=SCOPE).reconcile(
            spec.episode, case
        )
    assert len(data.trace(spec.episode)["orders"]) == 2
    assert len(data.trace(spec.episode)["fills"]) == 1


def test_incomplete_fills_keep_ledger_unchanged(database, maintenance):
    data, spec, venue, case = maintenance
    venue.trades = []
    with pytest.raises(ValueError, match="FILLS_NOT_CONFIRMED"):
        MaintenanceCloseReconciler(database, venue, scope=SCOPE).reconcile(
            spec.episode, case
        )
    assert len(data.trace(spec.episode)["orders"]) == 2


def test_cannot_bind_another_account_or_live(database, maintenance):
    _, _, venue, _ = maintenance
    for scope in (
        replace(SCOPE, account_id="other"),
        replace(SCOPE, environment="LIVE"),
    ):
        with pytest.raises(ValueError, match="BOUND_TESTNET"):
            MaintenanceCloseReconciler(database, venue, scope=scope)


def test_unconfirmed_receipt_cannot_bypass_close_reservation(database, maintenance):
    data, spec, venue, case = maintenance
    store = BusinessState(database)
    key = StateKey(
        **asdict(SCOPE), namespace="testnet-scoped-cleanup-v1", key=case + ":close"
    )
    store.change(
        key,
        expected_version=1,
        request_key="unconfirmed",
        payload={"status": "UNKNOWN"},
        reason="QA",
    )
    with pytest.raises(ValueError, match="CONFIRMED_MAINTENANCE_REQUIRED"):
        MaintenanceCloseReconciler(database, venue, scope=SCOPE).reconcile(
            spec.episode, case
        )
    assert len(data.trace(spec.episode)["orders"]) == 2
