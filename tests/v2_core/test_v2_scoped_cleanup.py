import json
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_testnet_cleanup import Venue

from services.v2_testnet_cleanup import SCOPE
from services.v2_testnet_scoped_cleanup import ScopedCleanup
from tests.support.v2_admission import database as database_fixture

database = database_fixture


class ScopedVenue(Venue):
    def __call__(self, method, path, params):
        if method == "GET" and path.endswith("openAlgoOrders"):
            return (
                []
                if self.cancelled
                else [
                    {
                        "algoId": 123,
                        "symbol": "UBUSDT",
                        "side": "SELL",
                        "positionSide": "BOTH",
                        "closePosition": True,
                        "algoStatus": "NEW",
                    }
                ]
            )
        result = super().__call__(method, path, params)
        if method == "GET" and path.endswith("algoOrder"):
            result["symbol"] = "UBUSDT"
        return result


def worker(database, venue, case=None, **kw):
    return ScopedCleanup(
        database,
        venue,
        scope=SCOPE,
        case_id=case or uuid4(),
        symbol="UBUSDT",
        approved_algos=[123],
        runner_stopped=kw.get("stopped", lambda: True),
        pause=lambda _: None,
    )


def test_scoped_close_then_cancel_and_restart_without_writes(database):
    venue, case = ScopedVenue(), uuid4()
    first = worker(database, venue, case).run()
    assert first["status"] == "TARGET_FLAT"
    assert first["historical_unknown_resolved"] is False
    assert [r[0] for r in venue.sent] == ["POST", "DELETE"]
    assert worker(database, venue, case).run() == first
    assert len(venue.sent) == 2


def test_timeout_never_resubmits_or_removes_protection(database):
    venue, case = ScopedVenue(), uuid4()
    venue.unavailable = True
    for _ in range(2):
        with pytest.raises(ValueError, match="CLOSE_UNCONFIRMED"):
            worker(database, venue, case).run()
    assert [r[0] for r in venue.sent] == ["POST"]
    assert venue.cancelled is False


def test_lost_response_queries_fill_before_cancelling_protection(database):
    venue = ScopedVenue()
    venue.lost_response = True
    assert worker(database, venue).run()["status"] == "TARGET_FLAT"
    assert [r[0] for r in venue.sent] == ["POST", "DELETE"]


def test_running_daemon_forbids_all_writes(database):
    venue = ScopedVenue()
    with pytest.raises(ValueError, match="NOT_STOPPED"):
        worker(database, venue, stopped=lambda: False).run()
    assert venue.sent == []


def test_case_cannot_be_rebound_to_another_symbol(database):
    venue, case = ScopedVenue(), uuid4()
    worker(database, venue, case).run()
    changed = ScopedCleanup(
        database,
        venue,
        scope=SCOPE,
        case_id=case,
        symbol="BTCUSDT",
        approved_algos=[123],
        runner_stopped=lambda: True,
        pause=lambda _: None,
    )
    with pytest.raises(ValueError, match="APPROVAL_CONFLICT"):
        changed.run()
    assert len(venue.sent) == 2


def test_changed_position_stops_before_submit(database):
    venue = ScopedVenue()
    cleanup = worker(database, venue)
    cleanup.plan()
    venue.position["positionAmt"] = "2000"
    with pytest.raises(ValueError, match="POSITION_CHANGED"):
        cleanup.run()
    assert venue.sent == []


def test_live_account_is_forbidden(database):
    venue = ScopedVenue()
    with pytest.raises(ValueError, match="TESTNET_FUTURES_ONLY"):
        ScopedCleanup(
            database,
            venue,
            scope=replace(SCOPE, environment="LIVE"),
            case_id=uuid4(),
            symbol="UBUSDT",
            approved_algos=[123],
            runner_stopped=lambda: True,
            pause=lambda _: None,
        )
    assert venue.sent == []


@pytest.mark.parametrize(
    "field,value",
    [("algoId", 456), ("closePosition", False), ("side", "BUY"), ("symbol", "BTCUSDT")],
)
def test_unapproved_protection_stops_before_write(database, field, value):
    class ChangedVenue(ScopedVenue):
        def __call__(self, method, path, params):
            result = super().__call__(method, path, params)
            if path.endswith("openAlgoOrders"):
                result[0][field] = value
            return result

    venue = ChangedVenue()
    with pytest.raises(ValueError, match="UNAPPROVED"):
        worker(database, venue).run()
    assert venue.sent == []


def terminal_protection_worker(*, status="EXPIRED", changes=None, position=False):
    """GET-only pure fixture; constructing the store never opens a connection."""
    raw = {
        "algoId": 123,
        "symbol": "UBUSDT",
        "algoStatus": status,
        "closePosition": True,
        "positionSide": "BOTH",
        "actualOrderId": "",
        **(changes or {}),
    }
    calls = []

    def request(method, path, params):
        assert method == "GET"
        calls.append((method, path, deepcopy(params)))
        if path.endswith("algoOrder"):
            return deepcopy(raw)
        if path.endswith("positionRisk"):
            return [{"symbol": "UBUSDT", "positionAmt": "1"}] if position else []
        if path.endswith("openOrders"):
            return []
        raise AssertionError("unexpected terminal verification endpoint")

    request.account_id, request.environment = SCOPE.account_id, SCOPE.environment

    def no_database():
        raise AssertionError("pure verification must not access a database")

    action = {
        "key": "cancel:123",
        "kind": "CANCEL",
        "symbol": "UBUSDT",
        "params": {"algoId": 123},
    }
    return worker(no_database, request), action, raw, calls


@pytest.mark.parametrize("status", ["EXPIRED", "REJECTED"])
def test_terminal_protection_preserves_true_status_without_claiming_cancel(status):
    cleanup, action, raw, calls = terminal_protection_worker(status=status)
    assert cleanup.verify(action) == {
        "order": raw,
        "terminal_protection_status": status,
        "cancellation_confirmed": False,
        "target_flat_verified": True,
    }
    assert all(method == "GET" for method, _, _ in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"algoId": 456},
        {"algoId": "123"},
        {"symbol": "BTCUSDT"},
        {"closePosition": False},
        {"closePosition": "true"},
        {"positionSide": "SHORT"},
        {"actualOrderId": "789"},
        {"actualOrderId": 789},
        {"actualOrderId": False},
        {"actualOrderId": None},
    ],
)
def test_terminal_protection_wrong_identity_or_child_is_not_resolved(changes):
    cleanup, action, _, _ = terminal_protection_worker(changes=changes)
    with pytest.raises(ValueError, match="ALGO_REQUIRES_RECONCILIATION"):
        cleanup.verify(action)


def test_terminal_protection_requires_target_flat():
    cleanup, action, _, _ = terminal_protection_worker(position=True)
    with pytest.raises(ValueError, match="TARGET_NOT_FLAT"):
        cleanup.verify(action)


def test_terminal_protection_missing_child_field_is_not_absence_proof():
    cleanup, action, raw, _ = terminal_protection_worker()
    raw.pop("actualOrderId")
    with pytest.raises(ValueError, match="ALGO_REQUIRES_RECONCILIATION"):
        cleanup.verify(action)


@pytest.mark.parametrize("status", ["NEW", "TRIGGERING", "TRIGGERED", "FINISHED"])
def test_terminal_protection_never_treats_execution_as_absent(status):
    cleanup, action, _, _ = terminal_protection_worker(status=status)
    with pytest.raises(ValueError, match="CANCEL_UNCONFIRMED"):
        cleanup.verify(action)


def test_terminal_protection_canceled_keeps_existing_proof_shape():
    cleanup, action, raw, _ = terminal_protection_worker(status="CANCELED")
    assert cleanup.verify(action) == {"order": raw}


def test_automatically_expired_unknown_cancel_recovers_by_reads_only(database):
    class ExpiringVenue(ScopedVenue):
        def __call__(self, method, path, params):
            if method == "GET" and self.position is None:
                if path.endswith("openAlgoOrders"):
                    return []
                if path.endswith("algoOrder"):
                    return {
                        "algoId": 123,
                        "symbol": "UBUSDT",
                        "algoStatus": "EXPIRED",
                        "closePosition": True,
                        "positionSide": "BOTH",
                        "actualOrderId": "",
                    }
            return super().__call__(method, path, params)

    venue, case = ExpiringVenue(), uuid4()
    cleanup = worker(database, venue, case)
    planned = cleanup.plan()
    assert cleanup.run_action(planned["actions"][0]) == "CONFIRMED"
    action = planned["actions"][1]
    for version, status in enumerate(("SENDING", "UNKNOWN")):
        assert (
            cleanup.store.change(
                cleanup.key(action),
                expected_version=version,
                request_key=status,
                payload={"action": action, "status": status},
                reason="QA_UNKNOWN_CANCEL",
            ).code
            == "APPLIED"
        )
    assert cleanup.run()["status"] == "TARGET_FLAT"
    saved = json.loads(cleanup.store.read(cleanup.key(action)).payload_json)
    assert saved["status"] == "CONFIRMED"
    assert saved["proof"]["order"]["algoStatus"] == "EXPIRED"
    assert saved["proof"]["cancellation_confirmed"] is False
    assert worker(database, venue, case).run()["status"] == "TARGET_FLAT"
    assert [method for method, _, _ in venue.sent] == ["POST"]
