from dataclasses import replace

import pytest
from test_v2_account_coverage import Account
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import evidence, intent
from test_v2_protection_child import SCOPE

from v2_core.protection import ProtectionSpec
from v2_core.protection_install import GuardedStopInstaller
from v2_core.service import TradingData
from v2_core.state import BusinessState

database = database_fixture


@pytest.fixture
def case(database, request):
    partial = getattr(request, "param", None) == "partial"
    filled = "0.005" if partial else "0.01"
    data = TradingData(database)
    original = intent()
    data.accept(original, evidence())
    order, _ = data.orders.prepare(original.intent_id)
    data.orders.transition(order, expected_version=1, status="SUBMITTING", evidence={})
    data.ledger.record_fill(
        order_id=order,
        exchange_fill_id="1",
        quantity=filled,
        price="100",
        fee="0.01",
        fee_currency="USDT",
        occurred_at_ms=1,
        evidence={"source": "test"},
    )
    data.orders.transition(
        order,
        expected_version=2,
        status="ACKNOWLEDGED" if partial else "FILLED",
        exchange_order_id="10",
        evidence={"fills_complete": True},
    )
    spec = ProtectionSpec(original.intent_id, "BTCUSDT", "SELL", "STOP_MARKET", "99")

    class Venue(Account):
        def __init__(self):
            super().__init__()
            self.writes, self.row, self.lose, self.absent = [], None, False, False
            self.rows["/fapi/v3/positionRisk"] = [
                {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": filled}
            ]

        def __call__(self, method, path, params):
            if method == "POST":
                assert (
                    path == "/fapi/v1/algoOrder" and params["closePosition"] == "true"
                )
                state, payload = installer.protection.read(spec)
                assert (
                    state
                    and payload["status"] == "SENDING"
                    and payload["creation_proof"]
                )
                self.writes.append(params)
                if not self.absent:
                    self.row = {
                        **params,
                        "orderType": params["type"],
                        "closePosition": True,
                        "priceProtect": False,
                        "algoId": 10,
                        "algoStatus": "NEW",
                    }
                if self.lose:
                    raise TimeoutError("secret")
                return self.row
            if path == "/fapi/v1/algoOrder":
                if self.row is None:
                    raise ValueError("not found")
                return dict(self.row)
            return super().__call__(method, path, params)

    venue = Venue()
    reference = {
        "symbol": "BTCUSDT",
        "environment": "SANDBOX",
        "mark_price": "100",
        "tick_size": "0.1",
        "observed_at_ms": 1000,
    }
    installer = GuardedStopInstaller(
        database,
        venue,
        scope=SCOPE,
        reference=lambda _: dict(reference),
        clock_ms=lambda: 1000,
        allow_create=True,
    )
    return data, spec, venue, reference, installer


def test_confirmed_exposure_installs_once_and_recovery_needs_no_reference(case):
    _, spec, venue, _, installer = case
    assert installer.ensure(spec)["status"] == "NEW"
    installer.allow_create = False
    installer.reference = lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
    assert installer.ensure(spec)["status"] == "NEW"
    assert len(venue.writes) == 1


def test_default_disabled_before_network(database, case):
    _, spec, venue, _, _ = case
    installer = GuardedStopInstaller(
        database, venue, scope=SCOPE, reference=lambda _: None, clock_ms=lambda: 1000
    )
    assert installer.ensure(spec) == {"status": "CREATION_DISABLED"}
    assert not venue.writes and not venue.calls


@pytest.mark.parametrize(
    "failure",
    [
        "external",
        "quantity",
        "missing",
        "wrongside",
        "take",
        "stale",
        "future",
        "wrongenv",
        "tick",
        "direction",
    ],
)
def test_unreconciled_or_invalid_protection_never_posts(case, failure):
    _, spec, venue, reference, installer = case
    if failure == "external":
        venue.rows["/fapi/v3/positionRisk"].append(
            {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "1"}
        )
    elif failure == "quantity":
        venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "0.02"
    elif failure == "missing":
        venue.rows["/fapi/v3/positionRisk"] = []
    elif failure == "wrongside":
        spec = replace(spec, side="BUY")
    elif failure == "take":
        spec = replace(spec, kind="TAKE_PROFIT_MARKET")
    elif failure == "stale":
        reference["observed_at_ms"] = -10000
    elif failure == "future":
        reference["observed_at_ms"] = 1001
    elif failure == "wrongenv":
        reference["environment"] = "LIVE"
    elif failure == "tick":
        spec = replace(spec, trigger_price="99.01")
    elif failure == "direction":
        spec = replace(spec, trigger_price="101")
    with pytest.raises(ValueError):
        installer.ensure(spec)
    assert not venue.writes


@pytest.mark.parametrize("absent", [False, True])
def test_lost_response_never_submits_twice(case, absent):
    _, spec, venue, _, installer = case
    venue.lose, venue.absent = True, absent
    if absent:
        with pytest.raises(ValueError):
            installer.ensure(spec)
        with pytest.raises(ValueError):
            installer.ensure(spec)
    else:
        assert installer.ensure(spec)["status"] == "NEW"
        assert installer.ensure(spec)["status"] == "NEW"
    assert len(venue.writes) == 1


def test_competing_close_before_registration_invalidates_facts(case):
    data, spec, venue, reference, installer = case

    def competing(_):
        data.orders.prepare(
            spec.episode, leg="CLOSE", quantity="0.01", request_key="racing-exit"
        )
        return reference

    installer.reference = competing
    with pytest.raises(ValueError, match="FACTS_CHANGED_BEFORE_REGISTRATION"):
        installer.ensure(spec)
    assert installer.protection.read(spec) == (None, None)
    assert not venue.writes


def test_pending_exit_blocks_installation(case):
    data, spec, venue, _, installer = case
    # Final first order, then a separately admitted in-flight close must block.
    data.orders.prepare(spec.episode, leg="CLOSE", quantity="0.01", request_key="exit")
    with pytest.raises(ValueError, match="ACCOUNT_NOT_RECONCILED"):
        installer.ensure(spec)
    assert not venue.writes


def test_expiring_proof_cannot_post(case):
    _, spec, venue, _, installer = case
    original = installer.reference

    def expiring(symbol):
        value = original(symbol)
        times = iter([1000, 12000])
        installer.clock = lambda: next(times)
        return value

    installer.reference = expiring
    with pytest.raises(ValueError, match="PROTECTION_PROOF_EXPIRED"):
        installer.ensure(spec)
    assert not venue.writes


def test_account_lock_blocks_installation_before_exchange(database, case):
    _, spec, venue, _, installer = case
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert installer.ensure(spec) == {"status": "BUSY"}
    assert not venue.calls and not venue.writes


def test_registration_failure_rolls_back_proof_before_post(case, monkeypatch):
    _, spec, venue, _, installer = case
    original = BusinessState.change

    def fail(self, key, **kwargs):
        result = original(self, key, **kwargs)
        if "creation_proof" in kwargs["payload"]:
            raise RuntimeError("injected commit failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(BusinessState, "change", fail)
        with pytest.raises(RuntimeError):
            installer.ensure(spec)
    assert installer.protection.read(spec) == (None, None)
    assert not venue.writes


def test_expiry_after_registration_remains_query_only(case):
    _, spec, venue, _, installer = case
    original = installer.reference

    def expiring(symbol):
        value = original(symbol)
        times = iter([1000, 1000, 12000])
        installer.clock = lambda: next(times)
        return value

    installer.reference = expiring
    with pytest.raises(ValueError, match="not found"):
        installer.ensure(spec)
    assert installer.protection.read(spec)[1]["status"] == "UNKNOWN"
    with pytest.raises(ValueError, match="not found"):
        installer.ensure(spec)
    assert not venue.writes


@pytest.mark.parametrize("case", ["partial"], indirect=True)
def test_partial_opening_explicitly_blocked(case):
    _, spec, venue, _, installer = case
    with pytest.raises(ValueError, match="ACCOUNT_NOT_RECONCILED"):
        installer.ensure(spec)
    assert not venue.writes
