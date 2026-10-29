"""Diagnostics never turn ambiguous exposure into fills or safety approval."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_external_close_recovery import setup as external_setup_fixture
from test_v2_protection_child import SCOPE
from test_v2_protection_child import database as database_fixture
from test_v2_protection_child import setup as original_setup_fixture

from v2_core.external_close_recovery import ExternalCloseRecovery
from v2_core.protection_child import ProtectionChildReconciler
from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.state import BusinessState, StateKey
from v2_core.transport import ExchangeTransportError

database = database_fixture
original_setup = original_setup_fixture
external_setup = external_setup_fixture


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0, "LT_5_MINUTES"),
        (299999, "LT_5_MINUTES"),
        (300000, "LT_1_HOUR"),
        (3599999, "LT_1_HOUR"),
        (3600000, "LT_1_DAY"),
        (86399999, "LT_1_DAY"),
        (86400000, "GE_1_DAY"),
        (864000000, "GE_1_DAY"),
    ],
)
def test_parent_age_bands_are_display_only_and_allowlisted(age, expected):
    worker = ProtectionChildReconciler.__new__(ProtectionChildReconciler)
    worker.clock_ms = lambda: 1000 + age
    result = worker._pending(
        "WAITING_CHILD_ID",
        {"algoStatus": "TRIGGERING", "updateTime": 1000, "secret": "NEVER_FORWARD"},
    )
    assert result["parent_update_age_band"] == expected
    assert result["parent_update_at_ms"] == 1000
    assert result["age_basis"] == "VENUE_PARENT_UPDATE_NOT_WAIT_START"
    assert result["execution_authorized"] is False
    assert result["child_order_id"] is None
    assert "NEVER_FORWARD" not in str(result)


@pytest.mark.parametrize("updated", [None, "1000", True, 0, -1, 1001, 1.5, {}])
def test_missing_invalid_or_future_parent_time_is_unknown(updated):
    worker = ProtectionChildReconciler.__new__(ProtectionChildReconciler)
    worker.clock_ms = lambda: 1000
    result = worker._pending(
        "WAITING_CHILD_ID", {"algoStatus": "secret", "updateTime": updated}
    )
    assert result["parent_status"] == "UNKNOWN"
    assert result["parent_update_at_ms"] is None
    assert result["parent_update_age_band"] == "UNKNOWN"
    assert "secret" not in str(result)


def test_waiting_child_diagnostics_keep_supervisor_dedup_stable(
    database, original_setup
):
    data, spec, venue, child = original_setup
    venue.parent.update(algoStatus="TRIGGERING", actualOrderId="", updateTime=1000)
    supervisor = ProtectionSupervisor(database, venue, scope=SCOPE)
    clock = [2000]
    supervisor.child.clock_ms = lambda: clock[0]
    identity = child.protection.key(spec).identity
    key = StateKey(
        exchange=SCOPE.exchange,
        account_id=SCOPE.account_id,
        environment=SCOPE.environment,
        product=SCOPE.product,
        namespace="protection-recovery-result-v1",
        key=identity,
    )
    first = supervisor.run_once()["results"][identity]
    version = BusinessState(database).read(key).version
    clock[0] += 1000
    assert supervisor.run_once()["results"][identity] == first
    assert BusinessState(database).read(key).version == version
    clock[0] = 301000
    changed = supervisor.run_once()["results"][identity]
    assert changed["status"] == "WAITING_CHILD_ID"
    assert changed["parent_update_age_band"] == "LT_1_HOUR"
    assert BusinessState(database).read(key).version == version + 1
    assert len(data.trace(spec.episode)["orders"]) == 1
    assert len(venue.calls) == 3


@pytest.mark.parametrize("raised", [False, True])
def test_missing_known_child_is_query_only_pending(original_setup, raised):
    data, spec, venue, worker = original_setup
    venue.parent.update(updateTime=1000)
    worker.clock_ms = lambda: 2000
    previous = worker.request

    def request(method, path, params):
        assert method == "GET"
        if path.endswith("/order"):
            if raised:
                raise ExchangeTransportError(
                    "EXCHANGE_RESPONSE_ERROR", status=400, code=-2013
                )
            return {"code": -2013, "msg": "NEVER_FORWARD"}
        return previous(method, path, params)

    worker.request = request
    result = worker.reconcile(spec)
    assert result["status"] == "WAITING_CHILD_VISIBILITY"
    assert result["child_order_id"] == "555"
    assert result["parent_status"] == "FINISHED"
    assert result["execution_authorized"] is False
    assert "NEVER_FORWARD" not in str(result)
    assert len(data.trace(spec.episode)["orders"]) == 1
    assert len(data.trace(spec.episode)["fills"]) == 1


def test_second_child_lookup_missing_keeps_parent_diagnostics(original_setup):
    data, spec, _venue, worker = original_setup
    worker.venue.query = lambda _: None
    result = worker.reconcile(spec)
    assert result["status"] == "WAITING_CHILD_VISIBILITY"
    assert result["child_order_id"] == "555"
    assert result["parent_update_age_band"] == "UNKNOWN"
    assert len(data.trace(spec.episode)["orders"]) == 1


@pytest.mark.parametrize("raised", [False, True])
def test_once_visible_child_missing_on_both_bound_lookups_is_not_adopted(
    original_setup, raised
):
    data, spec, venue, worker = original_setup
    lookups = []

    def request(method, path, params):
        assert method == "GET" and path.endswith("/order")
        lookups.append(params)
        if len(lookups) == 1:
            return dict(venue.child)
        if raised:
            raise ExchangeTransportError(
                "EXCHANGE_RESPONSE_ERROR", status=400, code=-2013
            )
        return {"code": -2013, "msg": "NEVER_FORWARD"}

    worker.request = request
    worker.venue.request = request
    result = worker.reconcile(spec)
    assert result["status"] == "WAITING_CHILD_VISIBILITY"
    assert result["child_order_id"] == "555"
    assert result["execution_authorized"] is False
    assert lookups == [
        {"symbol": "BTCUSDT", "orderId": 555},
        {"symbol": "BTCUSDT", "origClientOrderId": "exchange-generated"},
        {"symbol": "BTCUSDT", "orderId": 555},
    ]
    assert len(data.trace(spec.episode)["orders"]) == 1
    assert len(data.trace(spec.episode)["fills"]) == 1
    with worker.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='native-child-owner-v1'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("category", "status", "code"),
    [
        ("EXCHANGE_RESPONSE_ERROR", 503, -2013),
        ("NEVER_FORWARD", 400, -2013),
        ("EXCHANGE_RESPONSE_ERROR", "400", -2013),
        ("EXCHANGE_RESPONSE_ERROR", 400, -2013.0),
    ],
)
def test_child_not_found_requires_strict_transport_contract(
    original_setup, category, status, code
):
    _data, spec, _venue, worker = original_setup

    def request(*_args):
        raise ExchangeTransportError(category, status=status, code=code)

    worker.request = request
    with pytest.raises(ExchangeTransportError):
        worker.reconcile(spec)


@pytest.mark.parametrize("code", [-1003, -1021, None])
def test_other_child_lookup_failures_do_not_become_not_found(original_setup, code):
    _data, spec, _venue, worker = original_setup

    def request(*_args):
        raise ExchangeTransportError("QUOTA_DENIED", status=429, code=code)

    worker.request = request
    with pytest.raises(ExchangeTransportError):
        worker.reconcile(spec)


@pytest.mark.parametrize("pending_close", [False, True])
def test_nonzero_exposure_skip_is_explicit_and_never_account_clear(
    external_setup, pending_close
):
    data, spec, venue, worker = external_setup
    if pending_close:
        data.orders.prepare(
            spec.episode, leg="CLOSE", quantity="0.01", request_key="close"
        )
    venue.positions = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}
    ]
    result = worker.run_once()
    assert result["status"] == "CLEAR"  # Only the external-flat stage is clear.
    assert result["scope"] == "EXTERNAL_FLAT_RECONCILIATION_ONLY"
    assert result["account_safety_verified"] is False
    assert result["results"] == [] and result["candidate_count"] == 1
    assert result["skipped_count"] == 1
    assert result["skipped_with_nonfinal_orders_count"] == int(pending_close)
    assert result["skipped"] == [
        {
            "episode": spec.episode,
            "symbol": "BTCUSDT",
            "status": "SKIPPED",
            "reason": "VENUE_POSITION_NONZERO",
            "local_orders_final": not pending_close,
        }
    ]
    assert len(venue.calls) == 1
    assert len(data.trace(spec.episode)["fills"]) == 1
    assert BusinessState(worker.connect).read(worker._key(spec.episode)) is None


def test_flat_position_never_bypasses_unknown_local_close(external_setup):
    data, spec, _venue, worker = external_setup
    order, _ = data.orders.prepare(
        spec.episode, leg="CLOSE", quantity="0.01", request_key="close"
    )
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    result = worker.run_once()
    assert result["status"] == "BLOCKED"
    assert result["results"][0]["reason"] == "EXTERNAL_CLOSE_LOCAL_ORDERS_NOT_FINAL"
    assert result["account_safety_verified"] is False
    assert result["skipped_count"] == 0
    assert len(data.trace(spec.episode)["fills"]) == 1


def test_skip_details_are_bounded_without_hiding_count():
    candidates = [(str(uuid4()), f"COIN{i}USDT", i % 2 == 0) for i in range(9)]
    connection = SimpleNamespace(
        execute=lambda *_args: SimpleNamespace(fetchall=lambda: candidates)
    )

    class Venue:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __call__(self, method, path, _params):
            assert method == "GET" and path.endswith("positionRisk")
            return [
                {"symbol": name, "positionSide": "BOTH", "positionAmt": "1"}
                for _, name, _ in candidates
            ]

    worker = ExternalCloseRecovery(
        lambda: nullcontext(connection), Venue(), scope=SCOPE, clock_ms=lambda: 1000
    )
    result = worker.run_once(limit=3)
    assert len(result["skipped"]) == 3
    assert result["skipped_count"] == 9
    assert result["skipped_with_nonfinal_orders_count"] == 5
    assert result["skipped_truncated"] is True
    assert result["status"] == "CLEAR"
