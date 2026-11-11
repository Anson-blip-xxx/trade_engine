from contextlib import nullcontext
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_protection_child import setup as setup_fixture

from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.binance import BinanceFutures
from v2_core.errors import SubmissionNotSent
from v2_core.external_close_recovery import ExternalCloseRecovery
from v2_core.runner import ExecutionRunner
from v2_core.state import BusinessState, StateKey

database = database_fixture
original_setup = setup_fixture
SCOPE = AccountScope("BINANCE", "test-account", "SANDBOX", "FUTURES")


class ExternalVenue:
    account_id = SCOPE.account_id
    environment = SCOPE.environment

    def __init__(self, venue):
        self.venue, self.calls = venue, []
        self.positions, self.ordinary, self.conditional = [], [], []
        self.flip_on_final = False
        venue.child.update(
            type="LIMIT",
            price="101",
            timeInForce="IOC",
            clientOrderId="autoclose-123",
            updateTime=3,
        )

    def __call__(self, method, path, params):
        assert method == "GET"
        self.calls.append((method, path, dict(params)))
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("multiAssetsMargin"):
            return {"multiAssetsMargin": False}
        if path.endswith("positionRisk"):
            if (
                self.flip_on_final
                and sum(p.endswith("positionRisk") for _, p, _ in self.calls) > 2
            ):
                return [
                    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}
                ]
            return list(self.positions)
        if path.endswith("/account"):
            return {
                "totalWalletBalance": "100",
                "availableBalance": "100",
                "totalMarginBalance": "100",
            }
        if path.endswith("openOrders"):
            return self.ordinary
        if path.endswith("openAlgoOrders"):
            return self.conditional
        if path.endswith("/order"):
            return dict(self.venue.child)
        if path.endswith("userTrades"):
            return [dict(t) for t in self.venue.trades]
        raise AssertionError(path)


@pytest.fixture
def setup(original_setup, database):
    data, spec, original, _ = original_setup
    venue = ExternalVenue(original)
    worker = ExternalCloseRecovery(database, venue, scope=SCOPE, clock_ms=lambda: 100)
    return data, spec, venue, worker


def test_full_liquidation_atomic_adoption_replay_and_no_venue_writes(setup):
    data, spec, venue, worker = setup
    result = worker.reconcile(spec.episode)
    assert result["status"] == "RECONCILED"
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    close = next(o for o in trace["orders"] if o["leg"] == "CLOSE")
    assert close["status"] == "FILLED" and close["order_type"] == "LIMIT"
    assert close["request_evidence"]["kind"] == "LIQUIDATION"
    assert close["request_evidence"]["venue_order"]["clientOrderId"] == "autoclose-123"
    assert close["client_order_id"] != "autoclose-123"
    before = len(venue.calls)
    assert worker.reconcile(spec.episode)["status"] == "ALREADY_RECONCILED"
    assert len(venue.calls) == before
    assert len(data.trace(spec.episode)["fills"]) == 2
    protection = BusinessState(worker.connect).read(
        StateKey(
            **asdict(SCOPE),
            namespace="testnet-protection-v1",
            key='{"episode":"'
            + spec.episode
            + '","kind":"STOP_MARKET","symbol":"BTCUSDT"}',
        )
    )
    assert '"status":"EXTERNAL_FLAT_RECONCILED"' in protection.payload_json
    resolution = BusinessState(worker.connect).read(worker._key(spec.episode))
    assert '"remaining_quantity":"0"' in resolution.payload_json
    assert all(call[0] == "GET" for call in venue.calls)


def test_adapter_cannot_submit_adopted_liquidation_and_can_query_binding(setup):
    _data, spec, venue, worker = setup
    result = worker.reconcile(spec.episode)
    adapter = BinanceFutures(venue, account_id=SCOPE.account_id, environment="SANDBOX")
    runner = ExecutionRunner(
        worker.connect,
        submit=adapter.submit,
        query=adapter.query,
        risk_check=lambda _: False,
        scope=SCOPE,
    )
    snapshot = runner.snapshot(result["close_order_ids"][0])
    before = len(venue.calls)
    with pytest.raises(SubmissionNotSent):
        adapter.submit(snapshot)
    assert len(venue.calls) == before
    assert adapter.query(snapshot).client_order_id == snapshot["client_order_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"positionSide": "SHORT"},
        {"reduceOnly": False},
        {"origQty": "0.02"},
        {"executedQty": "0.005"},
        {"status": "PARTIALLY_FILLED"},
        {"orderId": 123},
    ],
)
def test_wrong_or_partial_order_never_adopted(setup, change):
    data, spec, venue, worker = setup
    venue.venue.child.update(change)
    with pytest.raises(ValueError):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "ETHUSDT"},
        {"side": "BUY"},
        {"positionSide": "SHORT"},
        {"qty": "0.005"},
        {"time": 101},
    ],
)
def test_bad_fill_proof_never_adopted(setup, change):
    data, spec, venue, worker = setup
    venue.venue.trades[0].update(change)
    with pytest.raises(ValueError):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_duplicate_fill_and_incomplete_history_fail_closed(setup):
    data, spec, venue, worker = setup
    venue.venue.trades.append(dict(venue.venue.trades[0]))
    with pytest.raises(ValueError, match="DUPLICATE"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_recovery_requires_flat_and_no_live_orders(setup):
    data, spec, venue, worker = setup
    venue.ordinary = [{"symbol": "BTCUSDT", "orderId": 44}]
    assert worker.reconcile(spec.episode)["status"] == "VENUE_EXPOSURE_OR_ORDERS"
    assert not any(path.endswith("userTrades") for _, path, _ in venue.calls)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_changed_venue_snapshot_fails_before_commit(setup):
    data, spec, venue, worker = setup
    venue.flip_on_final = True
    with pytest.raises(ValueError, match="VENUE_CHANGED"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_wrong_account_rejected_before_any_get(setup):
    _data, spec, venue, worker = setup
    with pytest.raises(ValueError, match="SCOPE"):
        ExternalCloseRecovery(
            worker.connect,
            venue,
            scope=replace(SCOPE, account_id="other"),
            clock_ms=lambda: 100,
        )
    venue.account_id = "other"
    wrong_account_worker = ExternalCloseRecovery(
        worker.connect,
        venue,
        scope=replace(SCOPE, account_id="other"),
        clock_ms=lambda: 100,
    )
    with pytest.raises(ValueError, match="SCOPE"):
        wrong_account_worker.reconcile(spec.episode)
    assert venue.calls == []


def test_pending_local_close_blocks_external_adoption(setup):
    data, spec, venue, worker = setup
    data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="pending"
    )
    with pytest.raises(ValueError, match="LOCAL_ORDERS_NOT_FINAL"):
        worker.reconcile(spec.episode)
    assert venue.calls == []


def test_foreign_owner_blocks_and_rolls_back(setup):
    data, spec, _venue, worker = setup
    BusinessState(worker.connect).change(
        StateKey(**asdict(SCOPE), namespace="native-child-owner-v1", key="BTCUSDT:555"),
        expected_version=0,
        request_key="bind",
        payload={"episode": "foreign", "binding": {}},
        reason="test",
    )
    with pytest.raises(ValueError, match="ALREADY_OWNED"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_final_marker_failure_rolls_back_all_closes(setup):
    from psycopg.errors import CheckViolation

    data, spec, _venue, worker = setup
    with worker.connect() as conn:
        conn.execute(
            "ALTER TABLE v2_state_history ADD CONSTRAINT fail_external_marker "
            "CHECK (request_key <> 'confirmed')"
        )
    with pytest.raises(CheckViolation):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1
    assert len(data.trace(spec.episode)["fills"]) == 1


def test_normal_emergency_close_also_retires_unknown_protection(setup):
    data, spec, venue, worker = setup
    order_id, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="emergency"
    )
    data.orders.transition(
        order_id, expected_version=1, status="SUBMITTING", evidence={}
    )
    data.ledger.record_fill(
        order_id=order_id,
        exchange_fill_id="22",
        quantity="0.01",
        price="99",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={"source": "test"},
    )
    data.orders.transition(
        order_id,
        expected_version=2,
        status="FILLED",
        exchange_order_id="222",
        evidence={"fills_complete": True},
    )
    result = worker.run_once()
    assert result["status"] == "CLEAR"
    assert result["results"][0]["status"] == "RECONCILED"
    assert result["results"][0]["close_order_ids"] == [order_id]
    assert not any(path.endswith("userTrades") for _, path, _ in venue.calls)
    assert len(data.trace(spec.episode)["orders"]) == 2


def test_run_once_skips_healthy_positions_with_one_read(setup):
    _data, _spec, venue, worker = setup
    venue.positions = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}
    ]
    assert worker.run_once()["status"] == "CLEAR"
    assert len(venue.calls) == 1


def test_run_once_does_not_replace_finished_native_protection(setup):
    from v2_core.protection import TestnetProtection

    data, spec, venue, worker = setup
    order_id, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="native-complete"
    )
    data.orders.transition(
        order_id, expected_version=1, status="SUBMITTING", evidence={}
    )
    data.ledger.record_fill(
        order_id=order_id,
        exchange_fill_id="23",
        quantity="0.01",
        price="99",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={"source": "test"},
    )
    data.orders.transition(
        order_id,
        expected_version=2,
        status="FILLED",
        exchange_order_id="223",
        evidence={"fills_complete": True},
    )
    protection = TestnetProtection(BusinessState(worker.connect), venue, scope=SCOPE)
    snapshot, payload = protection.read(spec)
    assert protection.save(spec, snapshot, {**payload, "status": "FINISHED"})
    result = worker.run_once()
    assert result["status"] == "CLEAR" and result["results"] == []
    assert result["scope"] == "EXTERNAL_FLAT_RECONCILIATION_ONLY"
    assert result["account_safety_verified"] is False
    assert result["candidate_count"] == 0 and result["skipped_count"] == 0
    assert venue.calls == []
    assert protection.read(spec)[1]["status"] == "FINISHED"
    assert BusinessState(worker.connect).read(worker._key(spec.episode)) is None
    assert worker.reconcile(spec.episode)["status"] == "RECONCILED"
    assert protection.read(spec)[1]["status"] == "FINISHED"


def test_registered_native_child_is_never_claimed_as_external(setup):
    from v2_core.protection import TestnetProtection

    data, spec, venue, worker = setup
    protection = TestnetProtection(BusinessState(worker.connect), venue, scope=SCOPE)
    snapshot, payload = protection.read(spec)
    assert protection.save(
        spec,
        snapshot,
        {
            **payload,
            "status": "FINISHED",
            "observation": {"actualOrderId": "555"},
        },
    )
    with pytest.raises(ValueError, match="NATIVE_CHILD_RECOVERY_REQUIRED"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1
    assert not any(path.endswith("/order") for _, path, _ in venue.calls)
    assert BusinessState(worker.connect).read(worker._key(spec.episode)) is None


def test_bounded_old_history_is_explicitly_blocked(setup):
    data, spec, _venue, worker = setup
    worker.clock_ms = lambda: 8 * 86400000
    worker.inventory.clock_ms = worker.clock_ms
    with pytest.raises(ValueError, match="HISTORY_WINDOW_EXCEEDED"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_fully_filled_but_partial_external_close_cannot_zero_whole_position(setup):
    data, spec, venue, worker = setup
    venue.venue.child.update(origQty="0.005", executedQty="0.005")
    venue.venue.trades[0]["qty"] = "0.005"
    with pytest.raises(ValueError, match="RESIDUAL_QUANTITY_MISMATCH"):
        worker.reconcile(spec.episode)
    assert len(data.trace(spec.episode)["orders"]) == 1


def test_healthy_positions_do_not_starve_later_external_close():
    healthy = [(str(uuid4()), f"COIN{i}USDT") for i in range(6)]
    target = (str(uuid4()), "BTCUSDT")
    candidates = [*healthy, target]

    class FakeConnection:
        def execute(self, _query, params):
            assert params[-1] == 1001
            return SimpleNamespace(
                fetchall=lambda: [
                    (episode, name, False) for episode, name in candidates
                ]
            )

    class FakeVenue:
        account_id = SCOPE.account_id
        environment = SCOPE.environment

        def __call__(self, method, path, params):
            assert method == "GET" and path.endswith("positionRisk") and params == {}
            return [
                {"symbol": name, "positionSide": "BOTH", "positionAmt": "1"}
                for _, name in healthy
            ]

    worker = ExternalCloseRecovery(
        lambda: nullcontext(FakeConnection()),
        FakeVenue(),
        scope=SCOPE,
        clock_ms=lambda: 100,
    )
    called = []

    def reconcile(episode):
        called.append(episode)
        return {"status": "RECONCILED", "episode": episode}

    worker.reconcile = reconcile
    assert worker.run_once(limit=5)["status"] == "CLEAR"
    assert called == [target[0]]


def test_production_or_unbound_transport_is_never_accepted():
    request = SimpleNamespace(account_id=SCOPE.account_id, environment="LIVE")
    with pytest.raises(ValueError, match="SCOPE"):
        ExternalCloseRecovery(
            None,
            request,
            scope=replace(SCOPE, environment="LIVE"),
            clock_ms=lambda: 100,
        )
