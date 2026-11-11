import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import v2_sandbox_maintenance_close as cli
from services.v2_testnet_cleanup import SCOPE
from tests.support.v2_admission import database as database_fixture
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.state import BusinessState
from v2_core.transport import ExchangeTransportError

database = database_fixture


class Venue:
    account_id = SCOPE.account_id
    environment = "SANDBOX"

    def __init__(self):
        self.position = {
            "symbol": "BRUSDT",
            "positionSide": "BOTH",
            "positionAmt": "-256",
            "entryPrice": "0.49",
            "updateTime": 1,
        }
        self.sent, self.reads, self.close = [], [], None
        self.unknown, self.lost_response = False, False

    def __call__(self, method, path, params):
        if method == "POST":
            self.sent.append(deepcopy(params))
            if not self.unknown:
                self.close, self.position = deepcopy(params), None
            if self.unknown or self.lost_response:
                raise TimeoutError("signed-url-api-key-must-not-escape")
            return {"orderId": 123}
        assert method == "GET"
        self.reads.append(path)
        if path.endswith("positionRisk"):
            return [] if self.position is None else [deepcopy(self.position)]
        if path.endswith(("openOrders", "openAlgoOrders")):
            return []
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": False}
        if path.endswith("algoOrder"):
            return {
                "symbol": "BRUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "clientAlgoId": params["clientAlgoId"],
                "algoStatus": "TRIGGERING",
                "actualOrderId": "",
            }
        if path.endswith("/order"):
            if self.close is None:
                return {"code": -2013}
            return {
                "symbol": "BRUSDT",
                "clientOrderId": self.close["newClientOrderId"],
                "side": "BUY",
                "positionSide": "BOTH",
                "reduceOnly": True,
                "type": "MARKET",
                "status": "FILLED",
                "orderId": 123,
                "origQty": "256",
                "executedQty": "256",
            }
        if path.endswith("userTrades"):
            return [
                {
                    "symbol": "BRUSDT",
                    "orderId": 123,
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "id": 456,
                    "qty": "256",
                    "commission": "0.05",
                    "realizedPnl": "-1",
                }
            ]
        raise AssertionError("unexpected fake venue endpoint")


def plan():
    return {
        "symbol": "BRUSDT",
        "actions": [
            {
                "kind": "CLOSE",
                "params": {
                    "symbol": "BRUSDT",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "type": "MARKET",
                    "quantity": "256",
                    "reduceOnly": "true",
                    "newClientOrderId": "v2" + uuid4().hex,
                    "newOrderRespType": "RESULT",
                },
            }
        ],
    }


def gate(venue=None, **kwargs):
    return cli.ExactCloseRequest(
        venue or Venue(),
        stopped=kwargs.get("stopped", lambda: True),
        route_unchanged=kwargs.get("route", lambda: True),
    )


def test_exact_single_reduce_only_post():
    venue, desired = Venue(), plan()
    request = gate(venue)
    request.bind(desired, lambda: True)
    request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert len(venue.sent) == 1 and venue.sent[0]["reduceOnly"] == "true"


@pytest.mark.parametrize(
    "field,value",
    [
        ("reduceOnly", "false"),
        ("symbol", "BTCUSDT"),
        ("side", "SELL"),
        ("quantity", "257"),
        ("newClientOrderId", "unbound"),
        ("type", "LIMIT"),
    ],
)
def test_plan_mismatch_never_reaches_transport(field, value):
    venue, desired = Venue(), plan()
    request = gate(venue)
    request.bind(desired, lambda: True)
    wrong = {**desired["actions"][0]["params"], field: value}
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request("POST", "/fapi/v1/order", wrong)
    assert venue.sent == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/order"),
        ("POST", "/fapi/v1/leverage"),
        ("POST", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/marginType"),
        ("GET", "/unknown"),
    ],
)
def test_other_writes_and_unknown_reads_unavailable(method, path):
    venue = Venue()
    with pytest.raises(ValueError):
        gate(venue)(method, path, {})
    assert not venue.sent and not venue.reads


@pytest.mark.parametrize("check", ["stopped", "route", "preflight"])
def test_late_gate_rejection_no_post(check):
    venue, desired = Venue(), plan()
    request = gate(venue, **({check: lambda: False} if check != "preflight" else {}))
    request.bind(desired, lambda: check != "preflight")
    with pytest.raises(ValueError, match="PREFLIGHT_FAILED"):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert venue.sent == []


def test_ambiguous_post_cannot_be_repeated():
    venue, desired = Venue(), plan()
    venue.unknown = True
    request = gate(venue)
    request.bind(desired, lambda: True)
    with pytest.raises(TimeoutError):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    with pytest.raises(ValueError, match="WRITE_DENIED"):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert len(venue.sent) == 1


@pytest.mark.parametrize(
    "check,expected_stage",
    [
        ("stopped", "RUNNER_STOP_CHECK"),
        ("route", "ROUTE_BINDING_CHECK"),
        ("preflight", "POSITION_PREFLIGHT"),
    ],
)
def test_failed_preflight_has_definitely_unsent_safe_diagnostic(check, expected_stage):
    venue, desired, events = Venue(), plan(), []
    request = gate(venue, **({check: lambda: False} if check != "preflight" else {}))
    request.bind(desired, lambda: check != "preflight", diagnostic=events.append)
    with pytest.raises(ValueError, match="PREFLIGHT_FAILED"):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert venue.sent == []
    assert len(events) == 1
    assert events[0]["stage"] == expected_stage
    assert events[0]["dispatch_certainty"] == "NOT_DISPATCHED"
    assert events[0]["transport_entered"] is False


@pytest.mark.parametrize(
    "category,status,code,certainty",
    [
        ("QUOTA_DENIED", None, None, "PRE_NETWORK_DENIED"),
        ("EXCHANGE_RESPONSE_ERROR", 400, -4131, "UNKNOWN"),
        ("NETWORK_OUTCOME_UNKNOWN", None, None, "UNKNOWN"),
    ],
)
def test_transport_failure_keeps_safe_category_without_claiming_missing_fill(
    category,
    status,
    code,
    certainty,
):
    class FailedVenue(Venue):
        def __call__(self, method, path, params):
            if method == "POST":
                raise ExchangeTransportError(category, status=status, code=code)
            return super().__call__(method, path, params)

    events, desired = [], plan()
    request = gate(FailedVenue())
    request.bind(desired, lambda: True, diagnostic=events.append)
    with pytest.raises(ExchangeTransportError):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert [event["event"] for event in events] == ["DISPATCH_READY", "DISPATCH_FAILED"]
    failed = events[-1]
    assert failed["stage"] == "TRANSPORT" and failed["transport_entered"] is True
    assert failed["dispatch_certainty"] == certainty
    assert failed["error"]["category"] == category
    if code is not None:
        assert failed["error"]["exchange_code"] == code
    assert desired["actions"][0]["params"]["newClientOrderId"] not in json.dumps(events)


def test_pre_send_diagnostic_storage_failure_blocks_transport():
    venue, desired, events = Venue(), plan(), []

    def unavailable(event):
        events.append(event)
        raise RuntimeError("secret DSN password")

    request = gate(venue)
    request.bind(desired, lambda: True, diagnostic=unavailable)
    with pytest.raises(RuntimeError):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert venue.sent == []
    assert events[-1]["stage"] == "PERSIST_PRE_SEND"
    assert events[-1]["dispatch_certainty"] == "NOT_DISPATCHED"
    assert "secret" not in json.dumps(events)


def test_diagnostic_failure_never_replaces_first_cause_or_retries():
    venue, desired, events = Venue(), plan(), []
    venue.unknown = True

    def recorder(event):
        events.append(event)
        if event["event"] == "DISPATCH_FAILED":
            raise RuntimeError("diagnostic storage unavailable")

    request = gate(venue)
    request.bind(desired, lambda: True, diagnostic=recorder)
    with pytest.raises(TimeoutError):
        request("POST", "/fapi/v1/order", desired["actions"][0]["params"])
    assert len(venue.sent) == 1
    assert "signed-url" not in json.dumps(events)


def systemd_output(**overrides):
    fields = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "ControlGroup": "",
        **overrides,
    }
    return SimpleNamespace(
        stdout="\n".join(key + "=" + value for key, value in fields.items())
    )


def test_stopped_requires_systemd_and_empty_descendant_cgroups(tmp_path):
    root = tmp_path / "system.slice" / cli.CONTROLLER
    child = root / "worker"
    child.mkdir(parents=True)
    (root / "cgroup.procs").write_text("")
    (child / "cgroup.procs").write_text("123\n")
    run = lambda *args, **kwargs: systemd_output()
    assert cli.runners_stopped(run=run, cgroup_root=tmp_path) is False
    (child / "cgroup.procs").write_text("")
    assert cli.runners_stopped(run=run, cgroup_root=tmp_path) is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("ActiveState", "active"),
        ("MainPID", "123"),
        ("SubState", "running"),
        ("LoadState", "not-found"),
        ("ControlGroup", "/outside/../system.slice"),
    ],
)
def test_any_unverified_runner_state_denies(tmp_path, field, value):
    assert not cli.runners_stopped(
        run=lambda *args, **kwargs: systemd_output(**{field: value}),
        cgroup_root=tmp_path,
    )


def test_systemd_failure_denies(tmp_path):
    def broken(*args, **kwargs):
        raise TimeoutError("must-not-escape")

    assert not cli.runners_stopped(run=broken, cgroup_root=tmp_path)


@pytest.mark.parametrize(
    "phase,epoch,expected",
    [
        ("ACTIVE", 46, 46),
        ("DRAINING", 47, 46),
        ("DRAINING", 46, 0),
    ],
)
def test_route_binding_rejection(phase, epoch, expected):
    with pytest.raises(ValueError):
        cli.checked_route([(str(uuid4()), str(uuid4()), 1, phase, epoch)], expected)


def test_unique_route_required():
    row = (str(uuid4()), str(uuid4()), 1, "DRAINING", 46)
    for rows in ([], [row, row]):
        with pytest.raises(ValueError, match="UNIQUE"):
            cli.checked_route(rows, 46)
    assert cli.checked_route([row], 46)[1]["epoch"] == 46


def test_controller_singleton_and_locked_binding_are_required(monkeypatch):
    row = (str(uuid4()), str(uuid4()), 1, "DRAINING", 46)
    seen, locks = [], []

    class Lease:
        allowed = True

        def execute(self, query, params):
            assert "pg_try_advisory_lock" in query
            assert params == ("execution-controller:" + row[0] + ":SANDBOX",)
            return SimpleNamespace(fetchone=lambda: (self.allowed,))

        def commit(self):
            seen.append("lease-committed")

    lease = Lease()

    @contextmanager
    def connect():
        yield lease

    @contextmanager
    def guarded():
        yield object()

    def rows(connection, *, locked=False):
        locks.append(locked)
        return [row]

    monkeypatch.setattr(cli, "execution_guard_connection", lambda connect: guarded)
    monkeypatch.setattr(cli, "route_rows", rows)
    monkeypatch.setattr(
        cli.ExecutionRoutes, "_binding", lambda *args: seen.append("binding-held")
    )
    with cli.maintenance_binding(connect, 46, lambda: True) as (_, target, unchanged):
        assert target["epoch"] == 46 and unchanged() is True
    assert locks == [False, True, False]
    assert seen == ["lease-committed", "binding-held"]
    lease.allowed = False
    with (
        pytest.raises(ValueError, match="CONTROLLER_ALREADY_RUNNING"),
        cli.maintenance_binding(connect, 46, lambda: True),
    ):
        pytest.fail("busy controller granted maintenance")


def test_stopped_check_precedes_database_access():
    with (
        pytest.raises(ValueError, match="NOT_STOPPED"),
        cli.maintenance_binding(
            lambda: pytest.fail("database touched"), 46, lambda: False
        ),
    ):
        pytest.fail("running worker granted maintenance")


def test_account_baseline_rejects_other_symbol_and_orders():
    venue = Venue()
    venue.position["symbol"] = "BTCUSDT"
    with pytest.raises(ValueError, match="OUTSIDE_APPROVED_SCOPE"):
        cli.account_baseline(venue)
    assert venue.sent == []

    class Orders(Venue):
        def __call__(self, method, path, params):
            if path.endswith("openOrders"):
                return [{"symbol": "BRUSDT"}]
            return super().__call__(method, path, params)

    with pytest.raises(ValueError, match="ORDINARY_ORDERS_PRESENT"):
        cli.account_baseline(Orders())


def test_approved_flag_and_symbol_before_database():
    for kwargs in (
        {"approved": False, "symbol": "BRUSDT"},
        {"approved": True, "symbol": "BTCUSDT"},
    ):
        with pytest.raises(ValueError, match="EXPLICIT_SCOPED"):
            cli.execute(case_id=str(uuid4()), expected_epoch=46, **kwargs)


def test_stdout_cannot_expose_exception_details(monkeypatch, capsys):
    def broken(**kwargs):
        raise RuntimeError("api-key-very-secret tenant-id credential-url")

    monkeypatch.setattr(cli, "execute", broken)
    assert (
        cli.main(
            [
                "--case-id",
                str(uuid4()),
                "--symbol",
                "BRUSDT",
                "--expected-route-epoch",
                "46",
                "--execute-approved-cleanup",
            ]
        )
        == 1
    )
    text = capsys.readouterr().out
    assert "secret" not in text and "tenant-id" not in text
    assert json.loads(text)["status"] == "CLEANUP_STOPPED"


def configure_execution(monkeypatch, venue):
    @contextmanager
    def binding(connect, epoch, stopped):
        assert epoch == 46 and stopped()
        yield "tenant-not-printed", {"binding_version": 1}, lambda: True

    def transport(**kwargs):
        assert kwargs["environment"] == "SANDBOX" and kwargs["enable_trading"] is True
        assert not any(
            kwargs.get(key)
            for key in (
                "enable_testnet_cancellation",
                "enable_testnet_order_cancellation",
                "enable_testnet_protection",
                "enable_testnet_settings",
            )
        )
        return venue

    monkeypatch.setattr(cli, "maintenance_binding", binding)
    monkeypatch.setattr(cli, "scope_for", lambda *args: SCOPE)
    monkeypatch.setattr(
        cli, "private_for", lambda *args: {"api_key": "qa", "api_secret": "qa"}
    )
    monkeypatch.setattr(cli, "HostBudget", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "PrivateRatePermit", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "BinanceSignedTransport", transport)


def run_case(database, case):
    return cli.execute(
        case_id=case,
        symbol="BRUSDT",
        expected_epoch=46,
        approved=True,
        connect=database,
        stopped=lambda: True,
    )


def test_pg_confirmed_maintenance_replay_no_new_post_and_parent_preserved(
    database, monkeypatch
):
    venue, case = Venue(), str(uuid4())
    configure_execution(monkeypatch, venue)
    store = BusinessState(database)
    protection = TestnetProtection(store, venue, scope=SCOPE)
    spec = ProtectionSpec(str(uuid4()), "BRUSDT", "BUY", "STOP_MARKET", "0.53")
    original = {"spec": cli.asdict(spec), "status": "TRIGGERING"}
    store.change(
        protection.key(spec),
        expected_version=0,
        request_key="seed",
        payload=original,
        reason="QA",
    )
    first = run_case(database, case)
    assert first["status"] == "TARGET_FLAT"
    assert first["unresolved_protection_count"] == 1
    assert first["historical_orders_resolved"] is False
    assert first["new_entries_authorized"] is False
    assert run_case(database, case) == first and len(venue.sent) == 1
    assert json.loads(store.read(protection.key(spec)).payload_json) == original
    with database() as connection:
        assert connection.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        count = connection.execute(
            "SELECT count(*) FROM v2_business_state WHERE scope->>'namespace'='testnet-scoped-cleanup-v1'"
        ).fetchone()[0]
    assert count >= 7


def test_pg_unknown_maintenance_replay_never_reposts(database, monkeypatch):
    venue, case = Venue(), str(uuid4())
    venue.unknown = True
    configure_execution(monkeypatch, venue)
    for _ in range(2):
        with pytest.raises(ValueError, match="CLOSE_UNCONFIRMED"):
            run_case(database, case)
    assert len(venue.sent) == 1
    with database() as connection:
        events = connection.execute(
            "SELECT h.payload FROM v2_state_history h JOIN v2_business_state s USING(state_id) "
            "WHERE s.scope->>'namespace'='testnet-scoped-cleanup-v1' "
            "AND s.scope->>'key'=%s ORDER BY h.version",
            (case + ":dispatch-diagnostic",),
        ).fetchall()
    assert [row[0]["event"] for row in events] == ["DISPATCH_READY", "DISPATCH_FAILED"]
    assert events[-1][0]["dispatch_certainty"] == "UNKNOWN"
    assert "signed-url" not in json.dumps(events)


def test_pg_lost_response_recovers_only_by_reads(database, monkeypatch):
    venue, case = Venue(), str(uuid4())
    venue.lost_response = True
    configure_execution(monkeypatch, venue)
    assert run_case(database, case)["status"] == "TARGET_FLAT"
    assert len(venue.sent) == 1
