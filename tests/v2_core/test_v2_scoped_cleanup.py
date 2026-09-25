from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_testnet_cleanup import Venue

from services.v2_testnet_cleanup import SCOPE
from services.v2_testnet_scoped_cleanup import ScopedCleanup

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
