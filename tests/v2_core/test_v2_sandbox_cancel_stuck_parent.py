import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import v2_sandbox_cancel_stuck_parent as cli
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.state import BusinessState, StateKey
from v2_core.transport import ExchangeTransportError

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-stuck-parent", "SANDBOX", "FUTURES")


class Venue:
    account_id, environment = SCOPE.account_id, SCOPE.environment

    def __init__(self, spec):
        self.spec = spec
        expected = TestnetProtection(None, self, scope=SCOPE).params(spec)
        self.parent = {
            **expected,
            "orderType": spec.kind,
            "algoId": 123456,
            "closePosition": True,
            "priceProtect": False,
            "algoStatus": "TRIGGERING",
            "actualOrderId": "",
        }
        self.positions = [
            {
                "symbol": "BRUSDT",
                "positionSide": "BOTH",
                "positionAmt": "-256",
                "entryPrice": "0.49",
                "updateTime": 1,
            }
        ]
        self.dual, self.ordinary, self.algos = False, [], []
        self.calls, self.sent, self.error, self.lost = [], [], None, False
        self.parent_read_hook = None

    def __call__(self, method, path, params):
        self.calls.append((method, path, deepcopy(params)))
        if method == "DELETE":
            assert path == "/fapi/v1/algoOrder" and params == {"algoId": 123456}
            self.sent.append(deepcopy(params))
            if self.error:
                raise self.error
            self.parent["algoStatus"] = "CANCELED"
            if self.lost:
                raise TimeoutError("never-persist-secret-url")
            return {"code": 200, "msg": "never-persist-response-text"}
        assert method == "GET"
        if path.endswith("positionSide/dual"):
            return {"dualSidePosition": self.dual}
        if path.endswith("positionRisk"):
            return deepcopy(self.positions)
        if path.endswith("openOrders"):
            return deepcopy(self.ordinary)
        if path.endswith("openAlgoOrders"):
            return deepcopy(self.algos)
        if path.endswith("algoOrder"):
            assert params in (
                {"algoId": 123456},
                {"clientAlgoId": self.parent["clientAlgoId"]},
            )
            if self.parent_read_hook:
                self.parent_read_hook(self)
            return deepcopy(self.parent)
        raise AssertionError("unknown endpoint")


@pytest.fixture
def case(database):
    spec = ProtectionSpec(str(uuid4()), "BRUSDT", "BUY", "STOP_MARKET", "0.50")
    venue = Venue(spec)
    store = BusinessState(database)
    reader = TestnetProtection(store, venue, scope=SCOPE)
    assert reader.save(
        spec,
        None,
        {"spec": asdict(spec), "status": "TRIGGERING", "observation": venue.parent},
    )
    quarantine = StateKey(
        **asdict(SCOPE), namespace="symbol-opening-quarantine-v1", key="BRUSDT"
    )
    assert (
        store.change(
            quarantine,
            expected_version=0,
            request_key="qa-halt",
            payload={"halted": True},
            reason="QA",
        ).code
        == "APPLIED"
    )
    runner = cli.StuckParentCancel(
        database,
        venue,
        scope=SCOPE,
        case_id=str(uuid4()),
        stopped=lambda: True,
        route_unchanged=lambda: True,
        clock_ms=lambda: 100,
    )
    return runner, venue, spec, quarantine


def gate(venue, events):
    request = cli.ExactParentRequest(
        venue,
        scope=SCOPE,
        algo_id=123456,
        client_id=venue.parent["clientAlgoId"],
        fence=lambda: events.append("fence") or True,
    )
    request.bind(
        lambda: events.append("preflight") or True,
        lambda: events.append("durable-latch") or True,
    )
    return request


def pure_venue():
    return Venue(ProtectionSpec(str(uuid4()), "BRUSDT", "BUY", "STOP_MARKET", "0.50"))


@pytest.mark.parametrize(
    "method,path,params",
    [
        ("POST", "/fapi/v1/order", {}),
        ("POST", "/fapi/v1/algoOrder", {}),
        ("DELETE", "/fapi/v1/order", {}),
        ("DELETE", "/fapi/v1/allOpenOrders", {}),
        ("DELETE", "/fapi/v1/algoOrder", {"algoId": 999}),
        ("DELETE", "/fapi/v1/algoOrder", {"algoId": 123456, "symbol": "BRUSDT"}),
        ("GET", "/fapi/v1/algoOrder", {"algoId": 999}),
        ("GET", "/fapi/v3/positionRisk", {"symbol": "BTCUSDT"}),
    ],
)
def test_only_exact_parent_delete_or_exact_reads(method, path, params):
    venue = pure_venue()
    with pytest.raises(cli.CancelRejected):
        gate(venue, [])(method, path, params)
    assert venue.calls == []


def test_durable_latch_precedes_one_delete_even_after_lost_response():
    venue, events = pure_venue(), []
    venue.lost = True
    request = gate(venue, events)
    with pytest.raises(TimeoutError):
        request("DELETE", "/fapi/v1/algoOrder", {"algoId": 123456})
    assert events == ["fence", "preflight", "durable-latch", "fence"]
    with pytest.raises(cli.CancelRejected, match="EXACT_PARENT_DELETE_ONLY"):
        request("DELETE", "/fapi/v1/algoOrder", {"algoId": 123456})
    assert len(venue.sent) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "BTCUSDT"),
        ("side", "SELL"),
        ("positionSide", "SHORT"),
        ("algoId", 999),
        ("algoId", True),
        ("clientAlgoId", "different"),
        ("orderType", "TAKE_PROFIT_MARKET"),
        ("type", "LIMIT"),
        ("closePosition", "true"),
        ("closePosition", False),
        ("priceProtect", True),
        ("workingType", "CONTRACT_PRICE"),
        ("triggerPrice", "0.51"),
        ("algoType", "OTHER"),
        ("algoStatus", "NEW"),
        ("algoStatus", "FINISHED"),
        ("actualOrderId", "123"),
        ("actualOrderId", 123),
        ("actualOrderId", False),
    ],
)
def test_identity_or_trigger_state_changes_refuse_cancellation(field, value):
    venue = pure_venue()
    expected = TestnetProtection(None, venue, scope=SCOPE).params(venue.spec)
    raw = {**venue.parent, field: value}
    with pytest.raises((cli.CancelRejected, ValueError, TypeError)):
        cli.checked_parent(
            raw, spec=venue.spec, expected=expected, algo_id=123456, stuck=True
        )


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/order"),
        ("GET", "/unknown"),
    ],
)
def test_transport_permit_does_not_enable_post_or_other_deletes(method, path):
    weights = []
    budget = SimpleNamespace(permit=lambda weight: weights.append(weight) or True)
    assert cli.DeleteOnlyPermit(budget)(method, path) is False
    assert weights == []


@pytest.mark.parametrize(
    "epoch,approved", [(45, True), (47, True), (46, False), (True, True)]
)
def test_cli_bound_to_approved_epoch_before_any_database_or_credentials(
    epoch, approved
):
    with pytest.raises(
        cli.CancelRejected, match="EXPLICIT_STUCK_PARENT_APPROVAL_REQUIRED"
    ):
        cli.execute(case_id=str(uuid4()), expected_epoch=epoch, approved=approved)


def test_safe_error_cannot_emit_raw_exception_or_overridden_diagnostics():
    class Malicious(ExchangeTransportError):
        def diagnostic_evidence(self):
            return {"secret": "API_KEY"}

    assert cli.safe_error(TimeoutError("API_KEY secret URL")) == {
        "category": "QUERY_OR_VALIDATION_ERROR"
    }
    assert cli.safe_error(Malicious("secret", status=400, code=-2011)) == {
        "category": "TRANSPORT_ERROR",
        "http_status": 400,
        "exchange_code": -2011,
    }


def test_live_binding_rejected_before_decrypting_credentials(monkeypatch):
    monkeypatch.setattr(
        cli,
        "maintenance_binding",
        lambda *args: nullcontext(("tenant", {}, lambda: True)),
    )
    monkeypatch.setattr(
        cli, "scope_for", lambda *args: replace(SCOPE, environment="LIVE")
    )
    calls = []
    monkeypatch.setattr(cli, "private_for", lambda *args: calls.append("credentials"))
    with pytest.raises(cli.CancelRejected, match="SANDBOX_SCOPE_REQUIRED"):
        cli.execute(
            case_id=str(uuid4()), expected_epoch=46, approved=True, connect=lambda: None
        )
    assert calls == []


def test_confirmed_cancel_preserves_parent_journal_and_does_not_claim_flat(case):
    runner, venue, spec, _ = case
    parent_key = TestnetProtection(runner.store, venue, scope=SCOPE).key(spec)
    before = runner.store.read(parent_key)
    result = runner.run()
    assert result["status"] == "CANCEL_CONFIRMED"
    assert result["exposure_flat_verified"] is False
    assert result["new_entries_authorized"] is False
    assert result["historical_orders_resolved"] is False
    assert runner.store.read(parent_key) == before
    assert venue.positions[0]["positionAmt"] == "-256"
    assert len(venue.sent) == 1
    runner.case_id = str(uuid4())
    assert runner.run()["status"] == "CANCEL_CONFIRMED"
    assert len(venue.sent) == 1  # case UUID is not cancellation identity
    assert all(method != "POST" for method, _, _ in venue.calls)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("never-persist-secret"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-2011),
        ExchangeTransportError("QUOTA_DENIED"),
    ],
)
def test_unknown_or_rejected_cancel_is_query_only_even_with_new_case(case, error):
    runner, venue, _, _ = case
    venue.error = error
    assert runner.run()["status"] == "QUERY_PENDING"
    venue.error = None
    runner.case_id = str(uuid4())
    assert runner.run()["status"] == "QUERY_PENDING"
    assert len(venue.sent) == 1
    with runner.connect() as conn:
        payloads = conn.execute(
            "SELECT payload FROM v2_business_state WHERE scope->>'namespace'='testnet-scoped-cleanup-v1'"
        ).fetchall()
    text = json.dumps(payloads)
    assert "never-persist-secret" not in text
    if isinstance(error, ExchangeTransportError) and error.code:
        assert '"http_status": 400' in text and '"exchange_code": -2011' in text


def test_lost_success_is_confirmed_only_by_get(case):
    runner, venue, _, _ = case
    venue.lost = True
    assert runner.run()["status"] == "CANCEL_CONFIRMED"
    assert venue.calls[-1][0] == "GET"
    assert len(venue.sent) == 1


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda v: setattr(v, "dual", True), "ONE_WAY_REQUIRED"),
        (lambda v: setattr(v, "positions", []), "EXACT_BR_EXPOSURE_REQUIRED"),
        (
            lambda v: v.positions[0].update(positionAmt="-255"),
            "EXACT_BR_EXPOSURE_REQUIRED",
        ),
        (
            lambda v: v.positions.append({**v.positions[0], "symbol": "BTCUSDT"}),
            "EXACT_BR_EXPOSURE_REQUIRED",
        ),
        (lambda v: setattr(v, "ordinary", [{"orderId": 1}]), "ORDINARY_ORDERS_PRESENT"),
        (lambda v: setattr(v, "algos", [{"algoId": 1}]), "CONDITIONAL_ORDERS_PRESENT"),
    ],
)
def test_account_scope_or_live_orders_block_every_delete(case, mutation, reason):
    runner, venue, _, _ = case
    mutation(venue)
    with pytest.raises(cli.CancelRejected, match=reason):
        runner.run()
    assert venue.sent == []


def test_missing_quarantine_blocks_before_any_venue_io(case):
    runner, venue, _, _ = case
    runner.scope = replace(SCOPE, account_id="unquarantined")
    with pytest.raises(cli.CancelRejected, match="QUARANTINE_REQUIRED"):
        runner.run()
    assert venue.calls == []


@pytest.mark.parametrize("halted", [False, "true", 1])
def test_quarantine_requires_literal_true(case, halted):
    runner, venue, _, key = case
    row = runner.store.read(key)
    runner.store.change(
        key,
        expected_version=row.version,
        request_key="qa-change",
        payload={"halted": halted},
        reason="QA",
    )
    with pytest.raises(cli.CancelRejected, match="QUARANTINE_REQUIRED"):
        runner.run()
    assert venue.calls == []


def test_multiple_known_nonfinal_parents_are_not_arbitrarily_selected(case):
    runner, venue, spec, _ = case
    other = replace(spec, episode=str(uuid4()))
    TestnetProtection(runner.store, venue, scope=SCOPE).save(
        other,
        None,
        {"spec": asdict(other), "status": "TRIGGERING", "observation": venue.parent},
    )
    with pytest.raises(cli.CancelRejected, match="UNIQUE_STUCK_PARENT_REQUIRED"):
        runner.run()
    assert venue.sent == []


def test_child_appearing_during_final_preflight_prevents_delete(case):
    runner, venue, _, _ = case
    calls = []

    def child(v):
        calls.append(1)
        if len(calls) >= 3:
            v.parent["actualOrderId"] = "77"

    venue.parent_read_hook = child
    with pytest.raises(cli.CancelRejected, match="ACTUAL_CHILD_PRESENT"):
        runner.run()
    assert venue.sent == []


def test_latch_commit_unknown_never_retries_delete(case, monkeypatch):
    runner, venue, _, _ = case
    original = runner.store.change

    def failure(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("request_key") == "send-once":
            raise TimeoutError("commit receipt lost")
        return result

    monkeypatch.setattr(runner.store, "change", failure)
    assert runner.run()["status"] == "QUERY_PENDING"
    monkeypatch.setattr(runner.store, "change", original)
    assert runner.run()["status"] == "QUERY_PENDING"
    assert venue.sent == []


def test_account_advisory_lock_excludes_other_workers(case):
    runner, venue, _, _ = case
    with runner.connect() as lock:
        lock.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        lock.commit()
        try:
            with pytest.raises(cli.CancelRejected, match="ACCOUNT_BUSY"):
                runner.run()
        finally:
            lock.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                ("testnet-protocol-account:" + SCOPE.key,),
            )
            lock.commit()
    assert venue.calls == []
