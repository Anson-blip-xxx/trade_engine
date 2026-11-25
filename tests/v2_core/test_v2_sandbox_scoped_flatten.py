"""Pure maintenance capability tests: no database, credentials, or venue I/O."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from scripts import v2_sandbox_scoped_flatten as cli
from services import v2_testnet_scoped_cleanup as cleanup_module
from v2_core.account_risk import AccountScope
from v2_core.state import StateSnapshot

CAMPAIGN = UUID("36bdcdb0-6986-4b31-8f70-221e477bca03")
REGISTRY = "d754d371-e04c-4da9-9a9e-08856a300eb1"


class Venue:
    account_id = "isolated-scoped-cleanup"
    environment = "SANDBOX"

    def __init__(self):
        self.calls = []
        self.timeout = False

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        if self.timeout and method != "GET":
            raise TimeoutError("isolated fake timeout")
        return {"confirmed_by_fake": True}


def plan(symbol="BRUSDT", *, approved_algos=(123,)):
    case = uuid5(CAMPAIGN, symbol)
    return {
        "symbol": symbol,
        "approved_algos": list(approved_algos),
        "actions": [
            {
                "key": "close",
                "kind": "CLOSE",
                "fingerprint": {"symbol": symbol, "positionAmt": "-256"},
                "params": {
                    "symbol": symbol,
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "quantity": "256",
                    "reduceOnly": "true",
                    "newClientOrderId": "v2" + uuid5(case, symbol).hex,
                    "newOrderRespType": "RESULT",
                },
            },
            *[
                {
                    "key": "cancel:" + str(identity),
                    "kind": "CANCEL",
                    "symbol": symbol,
                    "params": {"algoId": identity},
                }
                for identity in approved_algos
            ],
        ],
    }


def bound(venue=None, desired=None, *, stopped=None, route=None, preflight=None):
    venue = Venue() if venue is None else venue
    desired = plan() if desired is None else desired
    request = cli.ExactCleanupRequest(
        venue,
        stopped=stopped or (lambda: True),
        route_unchanged=route or (lambda: True),
    )
    request.bind(desired, preflight=preflight or (lambda _: True))
    return request, venue, desired


@pytest.mark.parametrize(
    "index,method,path",
    [(0, "POST", "/fapi/v1/order"), (1, "DELETE", "/fapi/v1/algoOrder")],
)
def test_exact_bound_action_is_sent_once(index, method, path):
    checked = []
    request, venue, desired = bound(
        preflight=lambda action: checked.append(action) is None
    )
    params = desired["actions"][index]["params"]
    assert request(method, path, params) == {"confirmed_by_fake": True}
    assert checked == [desired["actions"][index]]
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request(method, path, params)
    assert venue.calls == [(method, path, params)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "257"),
        ("quantity", "256.0"),
        ("side", "SELL"),
        ("symbol", "BTCUSDT"),
        ("positionSide", "SHORT"),
        ("type", "LIMIT"),
        ("reduceOnly", "false"),
        ("reduceOnly", True),
        ("newClientOrderId", "v2" + "0" * 32),
        ("newOrderRespType", "ACK"),
        ("unapproved", "extra"),
    ],
)
def test_close_parameter_changes_never_reach_transport(field, value):
    request, venue, desired = bound()
    params = {**desired["actions"][0]["params"], field: value}
    with pytest.raises(ValueError):
        request("POST", "/fapi/v1/order", params)
    assert venue.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"algoId": 124},
        {"algoId": "123"},
        {"algoId": True},
        {"algoId": 123, "symbol": "BRUSDT"},
    ],
)
def test_cancel_must_match_exact_bound_id_and_fields(params):
    request, venue, _ = bound()
    with pytest.raises(ValueError):
        request("DELETE", "/fapi/v1/algoOrder", params)
    assert venue.calls == []


@pytest.mark.parametrize(
    "method,path,params",
    [
        ("POST", "/fapi/v1/algoOrder", {}),
        ("POST", "/fapi/v1/leverage", {"symbol": "BRUSDT", "leverage": 20}),
        ("POST", "/fapi/v1/marginType", {"symbol": "BRUSDT", "marginType": "ISOLATED"}),
        ("DELETE", "/fapi/v1/order", {"orderId": 123}),
        ("DELETE", "/fapi/v1/allOpenOrders", {"symbol": "BRUSDT"}),
        ("PUT", "/fapi/v1/order", {}),
        ("GET", "/unapproved", {}),
    ],
)
def test_new_orders_settings_and_other_endpoints_are_unavailable(method, path, params):
    request, venue, _ = bound()
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request(method, path, params)
    assert venue.calls == []


@pytest.mark.parametrize("action_index", [0, 1])
@pytest.mark.parametrize("gate_name", ["stopped", "route", "preflight"])
def test_late_gate_denial_prevents_every_write(action_index, gate_name):
    checks = {name: True for name in ("stopped", "route", "preflight")}
    request, venue, desired = bound(
        stopped=lambda: checks["stopped"],
        route=lambda: checks["route"],
        preflight=lambda _: checks["preflight"],
    )
    checks[gate_name] = False  # Change after binding, not just at construction.
    method, path = (
        ("POST", "/fapi/v1/order")
        if action_index == 0
        else ("DELETE", "/fapi/v1/algoOrder")
    )
    with pytest.raises(ValueError, match="FENCED|PREFLIGHT_CHANGED"):
        request(method, path, desired["actions"][action_index]["params"])
    assert venue.calls == []


@pytest.mark.parametrize("action_index", [0, 1])
def test_transport_timeout_never_resends_exact_action(action_index):
    request, venue, desired = bound()
    venue.timeout = True
    method, path = (
        ("POST", "/fapi/v1/order")
        if action_index == 0
        else ("DELETE", "/fapi/v1/algoOrder")
    )
    params = desired["actions"][action_index]["params"]
    with pytest.raises(TimeoutError):
        request(method, path, params)
    venue.timeout = False
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request(method, path, params)
    assert len(venue.calls) == 1
    assert request("GET", "/fapi/v1/order", {"symbol": "BRUSDT"}) == {
        "confirmed_by_fake": True
    }


def test_bound_plan_is_detached_from_deep_caller_mutation():
    checked = []
    request, venue, desired = bound(
        preflight=lambda action: checked.append(action) is None
    )
    original = deepcopy(desired)
    desired["actions"][0]["params"]["quantity"] = "999"
    desired["actions"][0]["fingerprint"]["positionAmt"] = "-999"
    desired["actions"][1]["params"]["algoId"] = 999
    desired["approved_algos"].append(999)
    desired["actions"].clear()
    desired["symbol"] = "BTCUSDT"
    for index, method, path in (
        (0, "POST", "/fapi/v1/order"),
        (1, "DELETE", "/fapi/v1/algoOrder"),
    ):
        request(method, path, original["actions"][index]["params"])
    assert checked == original["actions"]
    assert venue.calls[0][2]["quantity"] == "256"
    assert venue.calls[1][2] == {"algoId": 123}


def test_mutating_preflight_copy_cannot_replace_the_saved_action():
    checked = []

    def preflight(action):
        checked.append(deepcopy(action))
        action["params"]["quantity"] = "999"
        action["fingerprint"]["positionAmt"] = "-999"
        return len(checked) > 1

    request, venue, desired = bound(preflight=preflight)
    params = desired["actions"][0]["params"]
    with pytest.raises(ValueError, match="PREFLIGHT_CHANGED"):
        request("POST", "/fapi/v1/order", params)
    request("POST", "/fapi/v1/order", params)
    assert checked == [desired["actions"][0], desired["actions"][0]]
    assert venue.calls == [("POST", "/fapi/v1/order", params)]


def test_unbound_or_rebound_request_never_grants_new_authority():
    venue, desired = Venue(), plan()
    request = cli.ExactCleanupRequest(
        venue, stopped=lambda: True, route_unchanged=lambda: True
    )
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    request.bind(desired, preflight=lambda _: True)
    with pytest.raises(ValueError, match="IMMUTABLE_PLAN"):
        request.bind(plan("BTCUSDT"), preflight=lambda _: True)
    assert venue.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("reduceOnly", "false"),
        ("type", "LIMIT"),
        ("quantity", "0"),
        ("positionSide", "SHORT"),
    ],
)
def test_binding_rejects_non_reducing_or_invalid_close(field, value):
    desired, venue = plan(), Venue()
    desired["actions"][0]["params"][field] = value
    with pytest.raises(ValueError):
        bound(venue, desired)
    assert venue.calls == []


@pytest.mark.parametrize("algo_id", [True, "123", 0, -1])
def test_binding_rejects_invalid_cancel_id(algo_id):
    desired, venue = plan(), Venue()
    desired["actions"][1]["params"]["algoId"] = algo_id
    desired["approved_algos"] = [algo_id]
    with pytest.raises(ValueError, match="EXACT_PROTECTION_ID_REQUIRED"):
        bound(venue, desired)
    assert venue.calls == []


def test_binding_rejects_unapproved_or_duplicate_actions():
    for change in ("unapproved_cancel", "duplicate", "no_close"):
        desired, venue = plan(), Venue()
        if change == "unapproved_cancel":
            desired["approved_algos"] = []
        elif change == "duplicate":
            desired["actions"].append(deepcopy(desired["actions"][0]))
        else:
            desired["actions"].pop(0)
        with pytest.raises(ValueError):
            bound(venue, desired)
        assert venue.calls == []


def test_live_transport_cannot_be_wrapped():
    venue = Venue()
    venue.environment = "LIVE"
    with pytest.raises(ValueError, match="SANDBOX_REQUIRED"):
        cli.ExactCleanupRequest(
            venue, stopped=lambda: True, route_unchanged=lambda: True
        )


class MemoryStore:
    """In-memory CAS port; the production cleanup action state machine is real."""

    def __init__(self):
        self.records = {}

    def read(self, key):
        return self.records.get(key.key)

    def change(self, key, *, expected_version, payload, deleted=False, **_kwargs):
        prior = self.read(key)
        if (prior.version if prior else 0) != expected_version:
            return SimpleNamespace(code="CONFLICT")
        self.records[key.key] = StateSnapshot(
            expected_version + 1, cli.canonical(payload), deleted
        )
        return SimpleNamespace(code="APPLIED")


class RecoveryVenue(Venue):
    def __init__(self, symbols):
        super().__init__()
        self.positions = {
            name: {
                "symbol": name,
                "positionSide": "BOTH",
                "positionAmt": "-256",
                "entryPrice": "0.49",
                "updateTime": 1,
            }
            for name in symbols
        }
        self.algos = {
            index: {
                "symbol": name,
                "side": "BUY",
                "positionSide": "BOTH",
                "closePosition": True,
                "algoStatus": "NEW",
                "algoId": index,
            }
            for index, name in enumerate(symbols, 123)
        }
        self.orders, self.writes = {}, []
        self.fail_close_symbol = None
        self.close_accepted = True
        self.fail_cancel_id = None
        self.before_write = lambda: None

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        name = params.get("symbol")
        if method != "GET":
            self.before_write()
            self.writes.append((method, path, deepcopy(params)))
        if method == "POST":
            if name != self.fail_close_symbol or self.close_accepted:
                self.positions.pop(name)
                self.orders[name] = {
                    "symbol": name,
                    "clientOrderId": params["newClientOrderId"],
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "reduceOnly": True,
                    "type": "MARKET",
                    "status": "FILLED",
                    "orderId": 900 + len(self.orders),
                    "origQty": "256",
                    "executedQty": "256",
                }
            if name == self.fail_close_symbol:
                raise TimeoutError("fake ambiguous close")
            return deepcopy(self.orders[name])
        if method == "DELETE":
            self.algos[params["algoId"]]["algoStatus"] = "CANCELED"
            if params["algoId"] == self.fail_cancel_id:
                raise TimeoutError("fake ambiguous cancellation")
            return deepcopy(self.algos[params["algoId"]])
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path == "/fapi/v3/positionRisk":
            return [
                deepcopy(row)
                for key, row in self.positions.items()
                if name in (None, key)
            ]
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v1/openAlgoOrders":
            return [
                deepcopy(row)
                for row in self.algos.values()
                if row["algoStatus"] == "NEW" and name in (None, row["symbol"])
            ]
        if path == "/fapi/v1/algoOrder":
            row = deepcopy(self.algos[params["algoId"]])
            if params["algoId"] == self.fail_cancel_id:
                row["algoStatus"] = "NEW"  # Delayed query visibility after timeout.
            return row
        if path == "/fapi/v1/order":
            if name == self.fail_close_symbol or name not in self.orders:
                return {"status": "NEW"}
            return deepcopy(self.orders[name])
        if path == "/fapi/v1/userTrades":
            return [
                {
                    "symbol": name,
                    "orderId": self.orders[name]["orderId"],
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "id": 1,
                    "qty": "256",
                    "commission": "0.05",
                    "realizedPnl": "-1",
                }
            ]
        raise AssertionError("unexpected fake endpoint")


@pytest.fixture
def execution(monkeypatch):
    names = ("BRUSDT", "BTCUSDT")
    store, venue = MemoryStore(), RecoveryVenue(names)
    scope = AccountScope("BINANCE", Venue.account_id, "SANDBOX", "FUTURES")

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *_: SimpleNamespace(fetchone=lambda: (True,)),
            commit=lambda: None,
        )

    @contextmanager
    def binding(_connect, epoch, _stopped):
        assert epoch == 34
        yield "isolated-tenant", {"target_registry": REGISTRY}, lambda: True

    def all_plans_saved():
        assert all(
            str(uuid5(CAMPAIGN, name)) + ":plan" in store.records for name in names
        )

    venue.before_write = all_plans_saved
    monkeypatch.setattr(cli, "database", lambda: connect)
    monkeypatch.setattr(cli, "maintenance_binding", binding)
    monkeypatch.setattr(cli, "scope_for", lambda *_: scope)
    monkeypatch.setattr(
        cli, "private_for", lambda *_: {"api_key": "fake", "api_secret": "fake"}
    )
    monkeypatch.setattr(cli, "HostBudget", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "BinanceSignedTransport", lambda **_kwargs: venue)
    monkeypatch.setattr(cli, "BusinessState", lambda _: store)
    monkeypatch.setattr(cleanup_module, "BusinessState", lambda _: store)
    monkeypatch.setattr(cli, "runners_stopped", lambda: True)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    def run(symbols=names, **overrides):
        return cli.execute(
            **{
                "expected_registry": REGISTRY,
                "expected_account": Venue.account_id,
                "expected_epoch": 34,
                "case_id": str(CAMPAIGN),
                "symbols": symbols,
                **overrides,
            }
        )

    return SimpleNamespace(run=run, store=store, venue=venue, scope=scope, names=names)


def test_execute_derives_stable_distinct_cases_and_finished_replay_is_read_only(
    execution,
):
    first = execution.run()
    assert first["status"] == "ACCOUNT_FLAT_VERIFIED"
    assert first["new_entries_authorized"] is False
    expected = [(name, str(uuid5(CAMPAIGN, name))) for name in execution.names]
    assert [(row["symbol"], row["case_id"]) for row in first["closed"]] == expected
    assert expected[0][1] != expected[1][1]
    writes = deepcopy(execution.venue.writes)
    assert execution.run() == first
    assert execution.venue.writes == writes
    assert all(
        execution.store.records[case + ":plan"].version == 1 for _, case in expected
    )


@pytest.mark.parametrize("closed_symbol", ["BRUSDT", "BTCUSDT"])
def test_first_run_requires_every_explicit_position_before_any_write(
    execution, closed_symbol
):
    execution.venue.positions.pop(closed_symbol)
    with pytest.raises(ValueError, match="APPROVED_POSITION_SET_CHANGED"):
        execution.run()
    assert execution.venue.writes == []
    assert execution.store.records == {}


def test_unapproved_account_position_denies_even_a_finished_campaign(execution):
    execution.run()
    writes = deepcopy(execution.venue.writes)
    execution.venue.positions["ETHUSDT"] = {"symbol": "ETHUSDT", "positionAmt": "1"}
    with pytest.raises(ValueError, match="APPROVED_POSITION_SET_CHANGED"):
        execution.run()
    assert execution.venue.writes == writes


def test_unknown_close_recovers_original_plan_without_reposting(execution):
    venue = execution.venue
    venue.fail_close_symbol = "BTCUSDT"
    with pytest.raises(ValueError, match="CLOSE_UNCONFIRMED"):
        execution.run()
    assert (
        venue.positions == {}
    )  # BR finished; BTC filled but response/query uncertain.
    plans = {
        key: value
        for key, value in execution.store.records.items()
        if key.endswith(":plan")
    }
    close_posts = [call for call in venue.writes if call[0] == "POST"]
    venue.fail_close_symbol = None
    assert execution.run()["status"] == "ACCOUNT_FLAT_VERIFIED"
    assert [call for call in venue.writes if call[0] == "POST"] == close_posts
    assert {key: execution.store.records[key] for key in plans} == plans


def test_unknown_unsuccessful_close_stays_query_only_across_restart(execution):
    venue = execution.venue
    venue.fail_close_symbol, venue.close_accepted = "BTCUSDT", False
    for _ in range(2):
        with pytest.raises(ValueError, match="CLOSE_UNCONFIRMED"):
            execution.run()
    assert set(venue.positions) == {"BTCUSDT"}
    assert (
        sum(
            call[0] == "POST" and call[2]["symbol"] == "BTCUSDT"
            for call in venue.writes
        )
        == 1
    )
    assert venue.algos[124]["algoStatus"] == "NEW"


def test_unknown_cancel_restores_original_ids_when_open_algo_list_is_empty(execution):
    venue = execution.venue
    venue.fail_cancel_id = 123
    with pytest.raises(ValueError, match="CANCEL_UNCONFIRMED"):
        execution.run()
    assert set(venue.positions) == {"BTCUSDT"}
    assert venue.algos[123]["algoStatus"] == "CANCELED"
    venue.fail_cancel_id = None
    assert execution.run()["status"] == "ACCOUNT_FLAT_VERIFIED"
    assert (
        sum(call[0] == "DELETE" and call[2]["algoId"] == 123 for call in venue.writes)
        == 1
    )


def test_saved_plan_does_not_authorize_a_different_flat_symbol(execution):
    execution.run()
    writes = deepcopy(execution.venue.writes)
    with pytest.raises(ValueError, match="APPROVED_POSITION_SET_CHANGED"):
        execution.run(symbols=(*execution.names, "ETHUSDT"))
    assert execution.venue.writes == writes


@pytest.mark.parametrize("deleted,version", [(True, 1), (False, 2)])
def test_deleted_or_rewritten_plan_is_never_restored(execution, deleted, version):
    execution.run()
    key = str(uuid5(CAMPAIGN, "BRUSDT")) + ":plan"
    original = execution.store.records[key]
    execution.store.records[key] = StateSnapshot(
        version, original.payload_json, deleted
    )
    writes = deepcopy(execution.venue.writes)
    with pytest.raises(ValueError, match="SAVED_CLEANUP_PLAN_INVALID"):
        execution.run()
    assert execution.venue.writes == writes


@pytest.mark.parametrize(
    "payload",
    ["{", "null", "[]", '{"symbol":"BRUSDT","approved_algos":[true],"actions":[{}]}'],
)
def test_malformed_saved_plan_denies_before_venue_write(execution, payload):
    key = str(uuid5(CAMPAIGN, "BRUSDT")) + ":plan"
    execution.store.records[key] = StateSnapshot(1, payload, False)
    with pytest.raises(ValueError, match="SAVED_CLEANUP_PLAN_INVALID"):
        execution.run()
    assert execution.venue.writes == []


@pytest.mark.parametrize(
    "error,expected",
    [
        (ValueError("CLOSE_UNCONFIRMED"), "CLOSE_UNCONFIRMED"),
        (ValueError("signed-url-secret"), "MAINTENANCE_CHECK_FAILED"),
        (RuntimeError("api-key-secret"), "MAINTENANCE_CHECK_FAILED"),
        (TimeoutError("signed-url-secret"), "VENUE_TIMEOUT"),
    ],
)
def test_main_only_emits_fixed_sanitized_error_codes(
    monkeypatch, capsys, error, expected
):
    def fail(**_kwargs):
        raise error

    monkeypatch.setattr(cli, "execute", fail)
    assert (
        cli.main(
            [
                "--expected-registry",
                REGISTRY,
                "--expected-account",
                Venue.account_id,
                "--case-id",
                str(CAMPAIGN),
                "--expected-epoch",
                "34",
                "--symbols",
                "BRUSDT",
                "--execute-approved-cleanup",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert expected in output
    assert (
        "secret" not in output
        and "signed-url" not in output
        and "api-key" not in output
    )
