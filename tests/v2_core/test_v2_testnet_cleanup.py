import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture

from services.v2_testnet_cleanup import SCOPE, Cleanup, make_plan, state_key
from v2_core.state import BusinessState

database = database_fixture
POSITION = {
    "symbol": "UBUSDT",
    "positionSide": "BOTH",
    "positionAmt": "1677",
    "entryPrice": "0.13051",
    "updateTime": 1,
}


def observation():
    return {
        "observation_id": str(uuid4()),
        "account_scope": asdict(SCOPE),
        "blockers": [],
        "responses": {
            "ordinary_orders": [],
            "conditional_orders": [{"algoId": 123, "symbol": "TUSDT"}],
            "positions_after": [dict(POSITION)],
        },
    }


class Venue:
    account_id = SCOPE.account_id
    environment = "SANDBOX"

    def __init__(self):
        self.position = dict(POSITION)
        self.sent = []
        self.cancelled = False
        self.close = None
        self.lost_response = False
        self.unavailable = False

    def __call__(self, method, path, params):
        if method == "POST":
            assert path == "/fapi/v1/order" and params["reduceOnly"] == "true"
            self.sent.append((method, path, params))
            if not self.unavailable:
                self.close = dict(params)
                self.position = None
            if self.lost_response or self.unavailable:
                raise TimeoutError("sensitive-request")
            return {"orderId": 1}
        if method == "DELETE":
            assert path == "/fapi/v1/algoOrder" and params == {"algoId": 123}
            self.sent.append((method, path, params))
            self.cancelled = True
            return {"algoId": 123, "code": "200"}
        assert method == "GET"
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("positionRisk"):
            return [] if self.position is None else [self.position]
        if path.endswith("openOrders"):
            return []
        if path.endswith("algoOrder"):
            return {
                "symbol": "TUSDT",
                "algoId": 123,
                "algoStatus": "CANCELED" if self.cancelled else "NEW",
            }
        if path.endswith("/order"):
            if self.close is None:
                return {"code": -2013}
            p = self.close
            return {
                "symbol": p["symbol"],
                "clientOrderId": p["newClientOrderId"],
                "side": p["side"],
                "positionSide": "BOTH",
                "reduceOnly": True,
                "type": "MARKET",
                "status": "FILLED",
                "orderId": 1,
                "origQty": p["quantity"],
                "executedQty": p["quantity"],
            }
        if path.endswith("userTrades"):
            return [
                {
                    "symbol": self.close["symbol"],
                    "orderId": 1,
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "id": 1,
                    "qty": self.close["quantity"],
                    "commission": "0.1",
                    "realizedPnl": "2",
                }
            ]
        raise AssertionError(path)


@pytest.mark.parametrize(
    "field,value",
    [("symbol", "BTCUSDT"), ("positionSide", "LONG"), ("positionAmt", "-1")],
)
def test_plan_rejects_positions_outside_approval(field, value):
    obs = observation()
    obs["responses"]["positions_after"][0][field] = value
    with pytest.raises(ValueError, match="UNAPPROVED"):
        make_plan(obs)


def test_plan_requires_no_new_ordinary_orders_or_unknown_reads():
    obs = observation()
    obs["blockers"] = ["INCOMPLETE_ACCOUNT_READ"]
    with pytest.raises(ValueError):
        make_plan(obs)
    obs = observation()
    obs["responses"]["ordinary_orders"] = [{"orderId": 99}]
    with pytest.raises(ValueError):
        make_plan(obs)


def test_close_then_cancel_are_durable_and_repeat_does_not_write(database):
    venue, store = Venue(), BusinessState(database)
    actions = make_plan(observation())["actions"]
    worker = Cleanup(store, venue)
    for action in actions:
        assert worker.run_action(action) == "CONFIRMED"
    for action in actions:
        assert Cleanup(BusinessState(database), venue).run_action(action) == "CONFIRMED"
    assert [s[0] for s in venue.sent] == ["POST", "DELETE"]
    worker.flat()


def test_close_response_lost_queries_original_identity_without_resubmit(database):
    venue = Venue()
    venue.lost_response = True
    action = make_plan(observation())["actions"][0]
    assert Cleanup(BusinessState(database), venue).run_action(action) == "CONFIRMED"
    assert len(venue.sent) == 1


def test_unresolved_close_never_retries_write_on_restart(database):
    venue = Venue()
    venue.unavailable = True
    action = make_plan(observation())["actions"][0]
    for _ in range(2):
        with pytest.raises(ValueError, match="UNCONFIRMED"):
            Cleanup(BusinessState(database), venue).run_action(action)
    assert len(venue.sent) == 1
    assert (
        "sensitive"
        not in BusinessState(database).read(state_key(action["key"])).payload_json
    )


def test_cannot_cancel_protection_while_any_position_remains(database):
    venue = Venue()
    action = make_plan(observation())["actions"][1]
    with pytest.raises(ValueError, match="NOT_FLAT"):
        Cleanup(BusinessState(database), venue).run_action(action)
    assert venue.sent == []


def test_changed_position_fingerprint_blocks_close(database):
    venue = Venue()
    action = make_plan(observation())["actions"][0]
    venue.position["updateTime"] = 2
    with pytest.raises(ValueError, match="CHANGED"):
        Cleanup(BusinessState(database), venue).run_action(action)
    assert venue.sent == []


def test_pg_failure_prevents_external_write():
    class FailedStore:
        def read(self, key):
            return None

        def change(self, *a, **k):
            raise ConnectionError("PG unavailable")

    venue = Venue()
    with pytest.raises(ConnectionError):
        Cleanup(FailedStore(), venue).run_action(make_plan(observation())["actions"][0])
    assert venue.sent == []


def test_live_environment_is_rejected():
    venue = Venue()
    venue.environment = "LIVE"
    with pytest.raises(ValueError, match="TESTNET"):
        Cleanup(None, venue)


def test_concurrent_action_claim_has_one_external_write(database):
    venue = Venue()
    action = make_plan(observation())["actions"][0]

    def attempt(_):
        try:
            return Cleanup(BusinessState(database), venue).run_action(
                copy.deepcopy(action)
            )
        except ValueError:
            return "QUERY_RACE"

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(attempt, range(4)))
    assert len(venue.sent) == 1


def test_cancel_eventual_visibility_retries_only_queries(database):
    class DelayedVenue(Venue):
        remaining = 2

        def __call__(self, method, path, params):
            raw = super().__call__(method, path, params)
            if (
                method == "GET"
                and path.endswith("algoOrder")
                and self.cancelled
                and self.remaining
            ):
                self.remaining -= 1
                return {**raw, "algoStatus": "NEW"}
            return raw

    venue = DelayedVenue()
    venue.position = None
    sleeps = []
    action = make_plan(observation())["actions"][1]
    assert (
        Cleanup(BusinessState(database), venue, pause=sleeps.append).run_action(action)
        == "CONFIRMED"
    )
    assert sleeps == [0.5, 0.5]
    assert len(venue.sent) == 1
