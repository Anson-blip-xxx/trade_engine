from dataclasses import replace

import pytest
from test_v2_intent_admission import database as database_fixture
from test_v2_transport import Connection, transport

from v2_core.account_risk import AccountScope
from v2_core.protection import ProtectionSpec, TestnetProtection
from v2_core.state import BusinessState
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa", "SANDBOX", "FUTURES")
SPEC = ProtectionSpec("episode-1", "BTCUSDT", "SELL", "STOP_MARKET", "50000")


class Venue:
    account_id = "qa"
    environment = "SANDBOX"

    def __init__(self):
        self.row = None
        self.writes = []
        self.lost = False
        self.position = "0"
        self.bad = {}

    def __call__(self, method, path, params):
        if method == "POST":
            self.writes.append(method)
            self.row = {
                **params,
                "orderType": params["type"],
                "closePosition": True,
                "priceProtect": False,
                "algoId": 42,
                "algoStatus": "NEW",
            }
            if self.lost:
                raise TimeoutError("secret")
        elif method == "DELETE":
            assert params == {"algoId": 42}
            self.writes.append(method)
            self.row["algoStatus"] = "CANCELED"
            if self.lost:
                raise TimeoutError("secret")
        elif path.endswith("positionRisk"):
            return [{"positionAmt": self.position}]
        elif self.row is None:
            raise ValueError("not found")
        return {**self.row, **self.bad}


def test_actual_lifecycle_and_restarts_do_not_repeat_writes(database):
    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    assert worker.submit_once(SPEC)["status"] == "NEW"
    assert worker.submit_once(SPEC)["status"] == "NEW"
    assert worker.cancel_flat_once(SPEC)["status"] == "CANCELED"
    assert worker.submit_once(SPEC)["status"] == "CANCELED"
    assert venue.writes == ["POST", "DELETE"]


def test_lost_create_and_cancel_responses_are_query_only(database):
    venue = Venue()
    venue.lost = True
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    assert worker.submit_once(SPEC)["status"] == "NEW"
    with pytest.raises(TimeoutError):
        worker.cancel_flat_once(SPEC)
    assert worker.cancel_flat_once(SPEC)["status"] == "CANCELED"
    assert venue.writes == ["POST", "DELETE"]


def test_prewrite_crash_not_found_does_not_resubmit(database):
    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    from dataclasses import asdict

    worker.save(SPEC, None, {"spec": asdict(SPEC), "status": "SENDING"})
    with pytest.raises(ValueError, match="not found"):
        worker.submit_once(SPEC)
    assert venue.writes == []


def test_changed_price_same_identity_rejected(database):
    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    worker.submit_once(SPEC)
    with pytest.raises(ValueError, match="identity conflict"):
        worker.submit_once(replace(SPEC, trigger_price="49999"))
    assert venue.writes == ["POST"]


def test_nonflat_account_cannot_cancel(database):
    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    worker.submit_once(SPEC)
    venue.position = "0.001"
    with pytest.raises(ValueError, match="flat"):
        worker.cancel_flat_once(SPEC)
    assert venue.writes == ["POST"]


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "ETHUSDT"},
        {"closePosition": False},
        {"triggerPrice": "1"},
        {"algoId": 43},
        {"priceProtect": True},
        {"algoStatus": "BOGUS"},
    ],
)
def test_bad_query_cannot_confirm_or_cancel(database, bad):
    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    worker.submit_once(SPEC)
    venue.bad = bad
    with pytest.raises(ValueError):
        worker.cancel_flat_once(SPEC)
    assert venue.writes == ["POST"]


def test_pg_failure_prevents_network_write():
    class Broken:
        def read(self, key):
            return None

        def change(self, *args, **kwargs):
            raise RuntimeError("offline")

    venue = Venue()
    worker = TestnetProtection(Broken(), venue, scope=SCOPE)
    with pytest.raises(RuntimeError):
        worker.submit_once(SPEC)
    assert venue.writes == []


def test_transport_gate_allows_only_testnet_close_all():
    conn = Connection()
    params = TestnetProtection(None, Venue(), scope=SCOPE).params(SPEC)
    with pytest.raises(ExchangeTransportError):
        transport(conn, enable_trading=True)("POST", "/fapi/v1/algoOrder", params)
    client = transport(conn, enable_testnet_protection=True)
    for bad in (
        {**params, "quantity": "1"},
        {**params, "closePosition": "false"},
        {**params, "type": "MARKET"},
        {**params, "positionSide": "LONG"},
    ):
        with pytest.raises(ExchangeTransportError):
            client("POST", "/fapi/v1/algoOrder", bad)
    assert conn.calls == []
    client("POST", "/fapi/v1/algoOrder", params)
    assert len(conn.calls) == 1
    with pytest.raises(ExchangeTransportError):
        client("POST", "/fapi/v1/order", {})


def test_live_protection_gate_rejected():
    with pytest.raises(ValueError):
        BinanceSignedTransport(
            account_id="qa",
            environment="LIVE",
            api_key="dummy",
            api_secret="dummy",
            clock_ms=lambda: 1,
            permit=lambda *_: True,
            enable_testnet_protection=True,
        )


def test_explicit_price_rejection_is_durable_and_not_retried(database):
    class Reject(Venue):
        def __call__(self, method, path, params):
            assert method == "POST"
            self.writes.append(method)
            raise ExchangeTransportError(
                "EXCHANGE_RESPONSE_ERROR", status=400, code=-4007
            )

    venue = Reject()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    for _ in range(2):
        assert worker.submit_once(SPEC)["status"] == "REJECTED"
    assert venue.writes == ["POST"]


def test_concurrent_submission_has_one_external_write(database):
    from concurrent.futures import ThreadPoolExecutor

    venue = Venue()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)

    def run():
        try:
            return worker.submit_once(SPEC)
        except ValueError:
            # A loser may read before the winner's POST has reached the venue,
            # or lose a CAS. Neither is permission to send another write.
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: run(), range(4)))
    assert venue.writes == ["POST"]
    assert worker.query(SPEC)["status"] == "NEW"


@pytest.mark.parametrize("expired", [False, True])
def test_cancel_rejection_requires_query_proof_of_expiry(database, expired):
    class AutoExpire(Venue):
        def __call__(self, method, path, params):
            if method == "DELETE":
                self.writes.append(method)
                if expired:
                    self.row["algoStatus"] = "EXPIRED"
                raise ExchangeTransportError(
                    "EXCHANGE_RESPONSE_ERROR", status=400, code=-2011
                )
            return super().__call__(method, path, params)

    venue = AutoExpire()
    worker = TestnetProtection(BusinessState(database), venue, scope=SCOPE)
    worker.submit_once(SPEC)
    if expired:
        assert worker.cancel_flat_once(SPEC)["status"] == "EXPIRED"
    else:
        with pytest.raises(ExchangeTransportError):
            worker.cancel_flat_once(SPEC)
        assert worker.cancel_flat_once(SPEC)["status"] == "CANCEL_PENDING_QUERY_ONLY"
    assert venue.writes == ["POST", "DELETE"]
