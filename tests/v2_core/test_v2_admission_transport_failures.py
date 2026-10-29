"""Real transport adapters with injected HTTP failures, never external traffic."""

import json

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import scenario
from test_v2_intent_admission import database as database_fixture
from test_v2_shared_market import shared as shared_fixture
from test_v2_transport import Connection

from services.v2_testnet_daemon_entry import PrivateRatePermit
from v2_core.public_market import BinancePublicMarket, PublicMarketError
from v2_core.scheduling import StrategyScheduler
from v2_core.shared_market import AdmissionBudget, HostBudget
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

database = database_fixture
shared = shared_fixture


def client(connect, response):
    return BinanceSignedTransport(
        account_id="qa",
        environment="SANDBOX",
        api_key="dummy-key",
        api_secret="dummy-secret",
        clock_ms=lambda: 1000,
        permit=PrivateRatePermit(
            AdmissionBudget(connect, environment="SANDBOX"),
            entries=False,
            protection=False,
            exits=False,
        ),
        connection_factory=lambda *_args, **_kwargs: response,
    )


@pytest.mark.parametrize("status,code", [(429, -1003), (418, -1003), (401, -2015)])
def test_signed_remote_failure_is_not_reclassified_as_local_wait(shared, status, code):
    response = Connection()
    response.status = status
    response.raw = json.dumps({"code": code, "msg": "sensitive-response"}).encode()
    with pytest.raises(ExchangeTransportError) as caught:
        client(shared, response)("GET", "/fapi/v3/account", {})
    assert caught.value.status == status and caught.value.code == code
    assert str(caught.value) == "EXCHANGE_RESPONSE_ERROR"
    assert len(response.calls) == 1 and response.closed
    # Venue bans remain shared across all lanes; auth errors are not rate bans.
    assert HostBudget(shared, environment="SANDBOX").permit(1) is (status == 401)


@pytest.mark.parametrize(
    "status,expected",
    [(429, "RATE_LIMITED"), (418, "RATE_LIMITED"), (401, "PUBLIC_HTTP_ERROR")],
)
def test_public_remote_failure_remains_visible(shared, status, expected):
    response = Connection()
    response.status = status
    transport = BinancePublicMarket(
        environment="SANDBOX",
        budget=AdmissionBudget(shared, environment="SANDBOX"),
        enabled=True,
        connection_factory=lambda *_args, **_kwargs: response,
    )
    with pytest.raises(PublicMarketError, match=expected):
        transport("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
    assert len(response.calls) == 1 and response.closed
    assert HostBudget(shared, environment="SANDBOX").permit(1) is (status == 401)


@pytest.mark.parametrize("status,code", [(429, -1003), (401, -2015)])
def test_scheduler_preserves_remote_failure_without_order_or_decision(
    shared, status, code
):
    _, runtime, signal, _ = scenario(shared)
    worker = formal(runtime, "S6")
    response = Connection()
    response.status = status
    response.raw = json.dumps({"code": code, "msg": "sensitive-response"}).encode()
    request = client(shared, response)
    scheduler = StrategyScheduler(
        worker, context_provider=lambda _: request("GET", "/fapi/v3/account", {})
    )
    assert scheduler.run_once(limit=1)[signal] == "UNAVAILABLE"
    assert worker.decision(signal) is None
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM v2_signal_receipts").fetchone() == (
            0,
        )
        assert conn.execute(
            "SELECT error_code FROM v2_strategy_tasks WHERE signal_id=%s", (signal,)
        ).fetchone() == ("EXCHANGE_RESPONSE_ERROR",)
