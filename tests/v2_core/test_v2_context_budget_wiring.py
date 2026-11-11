"""Explicit admission tickets reach every GET, without venue or database I/O."""

import json
import os
import threading
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from test_v2_shared_market import shared as shared_fixture

from services import v2_directional_account_context as context_module
from services.v2_testnet_daemon_entry import PrivateRatePermit
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.guarded_transport import GuardedSignedRequest
from v2_core.public_market import BinancePublicMarket, PublicMarketError
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError

database = database_fixture
shared = shared_fixture


class Budget:
    def __init__(self):
        self.permits, self.consumed, self.penalties = [], [], []
        self.revalidated = []
        self.failure = None

    def permit(self, weight):
        self.permits.append(weight)
        return True

    def consume_context(self, ticket, **scope):
        self.consumed.append((ticket, scope))
        if self.failure:
            raise self.failure
        if scope["account_id"] != "qa-context":
            raise ValueError("ADMISSION_CONTEXT_TICKET_INVALID")
        return True

    def penalize(self, seconds):
        self.penalties.append(seconds)

    def revalidate_context(self, ticket, **scope):
        self.revalidated.append((ticket, scope))
        if self.failure:
            raise self.failure
        return True


class HTTP:
    status = 200

    def __init__(self):
        self.connections, self.requests = [], []
        self.on_connect = lambda: None
        self.closed = False

    def __call__(self, host, *, timeout):
        self.connections.append((host, timeout))
        return self

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def connect(self):
        self.on_connect()

    def getresponse(self):
        return self

    def read(self, _limit):
        return b"{}"

    def getheader(self, _key, _default):
        return "60"

    def close(self):
        self.closed = True


def signed(budget, http, *, clock=lambda: 1000):
    return BinanceSignedTransport(
        account_id="qa-context",
        environment="SANDBOX",
        api_key="qa-key",
        api_secret="qa-secret",
        clock_ms=clock,
        permit=PrivateRatePermit(budget, entries=True, protection=True, exits=True),
        enable_trading=True,
        enable_testnet_settings=True,
        enable_testnet_cancellation=True,
        connection_factory=http,
    )


def public(budget, http):
    return BinancePublicMarket(
        environment="SANDBOX", budget=budget, enabled=True, connection_factory=http
    )


def test_signed_ticket_uses_explicit_bound_account_and_exact_weight_once():
    budget, http, ticket = Budget(), HTTP(), object()
    client = signed(budget, http)
    assert client("GET", "/fapi/v3/account", {}, admission_ticket=ticket) == {}
    assert not budget.permits
    assert budget.consumed == [
        (
            ticket,
            {
                "account_id": "qa-context",
                "environment": "SANDBOX",
                "method": "GET",
                "path": "/fapi/v3/account",
                "params": {},
                "weight": 5,
            },
        )
    ]
    assert len(http.requests) == 1


def test_public_ticket_requires_callers_explicit_account_and_exact_weight():
    budget, http, ticket = Budget(), HTTP(), object()
    client = public(budget, http)
    assert (
        client(
            "/fapi/v1/ticker/bookTicker",
            {"symbol": "BTCUSDT"},
            admission_ticket=ticket,
            account_id="qa-context",
        )
        == {}
    )
    assert not budget.permits
    assert budget.consumed == [
        (
            ticket,
            {
                "account_id": "qa-context",
                "environment": "SANDBOX",
                "method": "GET",
                "path": "/fapi/v1/ticker/bookTicker",
                "params": {"symbol": "BTCUSDT"},
                "weight": 2,
            },
        )
    ]
    for account in (None, "other-account"):
        with pytest.raises(ValueError, match="^ADMISSION_CONTEXT_TICKET_INVALID$"):
            client(
                "/fapi/v1/exchangeInfo", {}, admission_ticket=ticket, account_id=account
            )
    assert len(http.connections) == 1


def test_non_ticket_paths_keep_original_per_request_charging():
    budget, http = Budget(), HTTP()
    signed(budget, http)("GET", "/fapi/v3/account", {})
    public(budget, http)("/fapi/v1/exchangeInfo", {})
    assert budget.permits == [5, 1]
    assert not budget.consumed


@pytest.mark.parametrize(
    "route", ["/fapi/v1/order", "/fapi/v1/openOrders", "/fapi/v1/income"]
)
def test_private_ticket_cannot_authorize_extra_read_routes(route):
    budget, http = Budget(), HTTP()
    with pytest.raises(ValueError, match="^ADMISSION_CONTEXT_TICKET_INVALID$"):
        signed(budget, http)("GET", route, {}, admission_ticket=object())
    assert not budget.consumed and not budget.permits and not http.connections


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/leverage"),
        ("DELETE", "/fapi/v1/algoOrder"),
    ],
)
def test_ticket_can_never_authorize_signed_write_or_acquire_write_guard(
    guarded, method, path
):
    budget, http = Budget(), HTTP()
    client = signed(budget, http)
    if guarded:
        client = GuardedSignedRequest(
            client,
            account_id="qa-context",
            environment="SANDBOX",
            guard=lambda **_: pytest.fail("ticket must not acquire a write guard"),
        )
    with pytest.raises(ValueError, match="^ADMISSION_CONTEXT_TICKET_INVALID$"):
        client(method, path, {}, admission_ticket=object())
    assert not budget.consumed and not budget.permits and not http.connections


def test_guarded_get_forwards_ticket_without_write_guard():
    budget, http, ticket = Budget(), HTTP(), object()
    client = GuardedSignedRequest(
        signed(budget, http),
        account_id="qa-context",
        environment="SANDBOX",
        guard=lambda **_: pytest.fail("GET never acquires write guard"),
    )
    client("GET", "/fapi/v3/account", {}, admission_ticket=ticket)
    assert budget.consumed[0][0] is ticket


@pytest.mark.parametrize("port", ["signed", "public"])
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("ADMISSION_BUDGET_UNAVAILABLE"),
        ConnectionError("QA database unavailable"),
    ],
)
def test_ticket_check_failures_propagate_without_network_or_double_charge(
    port, failure
):
    budget, http = Budget(), HTTP()
    budget.failure = failure
    with pytest.raises(type(failure)) as caught:
        if port == "signed":
            signed(budget, http)(
                "GET", "/fapi/v3/account", {}, admission_ticket=object()
            )
        else:
            public(budget, http)(
                "/fapi/v1/exchangeInfo",
                {},
                admission_ticket=object(),
                account_id="qa-context",
            )
    assert caught.value is failure
    assert not http.connections and not budget.permits


def test_signed_ticket_revalidated_after_post_connect_clock_work():
    budget, http = Budget(), HTTP()

    def advanced_clock():
        budget.failure = ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        return 60000

    with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
        signed(budget, http, clock=advanced_clock)(
            "GET", "/fapi/v3/account", {}, admission_ticket=object()
        )
    assert len(http.connections) == 1 and http.closed and not http.requests
    assert len(budget.consumed) == len(budget.revalidated) == 1


def test_ticket_signed_timestamp_generated_after_six_second_handshake():
    budget, http, clock = Budget(), HTTP(), SimpleNamespace(now=1000)
    http.on_connect = lambda: setattr(clock, "now", 7000)
    signed(budget, http, clock=lambda: clock.now)(
        "GET", "/fapi/v3/account", {}, admission_ticket=object()
    )
    target = http.requests[0][0][1]
    assert parse_qs(urlsplit(target).query)["timestamp"] == ["7000"]
    assert len(budget.consumed) == len(budget.revalidated) == 1


@pytest.mark.parametrize(
    "failure",
    [ValueError("millisecond clock required"), ConnectionError("QA clock failure")],
)
def test_ticket_post_connect_clock_failure_preserves_local_validation(failure):
    budget, http = Budget(), HTTP()

    def clock():
        raise failure

    with pytest.raises(type(failure)) as caught:
        signed(budget, http, clock=clock)(
            "GET", "/fapi/v3/account", {}, admission_ticket=object()
        )
    assert caught.value is failure
    assert http.closed and not http.requests and not budget.revalidated


@pytest.mark.parametrize("port", ["signed", "public"])
def test_ticket_endpoint_params_cannot_change_during_connect(port):
    budget, http = Budget(), HTTP()
    params = {"symbol": "BTCUSDT"}
    http.on_connect = lambda: params.update(symbol="ETHUSDT")
    if port == "signed":
        signed(budget, http)(
            "GET", "/fapi/v1/symbolConfig", params, admission_ticket=object()
        )
    else:
        public(budget, http)(
            "/fapi/v1/premiumIndex",
            params,
            admission_ticket=object(),
            account_id="qa-context",
        )
    target = http.requests[0][0][1]
    assert parse_qs(urlsplit(target).query)["symbol"] == ["BTCUSDT"]
    assert budget.consumed[0][1]["params"] == {"symbol": "BTCUSDT"}


@pytest.mark.parametrize("port", ["signed", "public"])
@pytest.mark.parametrize(
    "failure",
    [
        ValueError("ADMISSION_BUDGET_UNAVAILABLE"),
        ConnectionError("QA DB unavailable after TLS"),
    ],
)
def test_connect_delay_revalidated_before_http_and_connection_is_closed(port, failure):
    budget, http = Budget(), HTTP()
    http.on_connect = lambda: setattr(budget, "failure", failure)
    with pytest.raises(type(failure)) as caught:
        if port == "signed":
            signed(budget, http)(
                "GET", "/fapi/v3/account", {}, admission_ticket=object()
            )
        else:
            public(budget, http)(
                "/fapi/v1/exchangeInfo",
                {},
                admission_ticket=object(),
                account_id="qa-context",
            )
    assert caught.value is failure
    assert len(budget.consumed) == len(budget.revalidated) == 1
    assert http.closed and not http.requests and not budget.permits


@pytest.mark.parametrize("port", ["signed", "public"])
def test_failed_connect_is_sanitized_not_reclassified_as_local_quota(port):
    budget, http = Budget(), HTTP()

    def fail():
        raise TimeoutError("sensitive DNS/TLS details")

    http.on_connect = fail
    with pytest.raises((ExchangeTransportError, PublicMarketError)) as caught:
        if port == "signed":
            signed(budget, http)(
                "GET", "/fapi/v3/account", {}, admission_ticket=object()
            )
        else:
            public(budget, http)(
                "/fapi/v1/exchangeInfo",
                {},
                admission_ticket=object(),
                account_id="qa-context",
            )
    assert "sensitive" not in str(caught.value)
    assert http.closed and not http.requests and not budget.revalidated


@pytest.mark.parametrize("port", ["signed", "public"])
def test_ticket_requests_still_penalize_actual_rate_limits(port):
    budget, http = Budget(), HTTP()
    http.status = 429
    with pytest.raises((ExchangeTransportError, PublicMarketError)):
        if port == "signed":
            signed(budget, http)(
                "GET", "/fapi/v3/account", {}, admission_ticket=object()
            )
        else:
            public(budget, http)(
                "/fapi/v1/exchangeInfo",
                {},
                admission_ticket=object(),
                account_id="qa-context",
            )
    assert budget.penalties == [60]
    assert not budget.permits


class HistoryReached(RuntimeError):
    pass


def context(monkeypatch, *, adaptive=False):
    """Stop after both fresh position reads; all DB policy ports are strict fakes."""
    from v2_core import adaptive_leverage, entry_controls, runtime_policy

    events, reserved, reads = [], [], []
    profile = SimpleNamespace(
        values={"leverage.adaptive_enabled": adaptive, "context.read_workers": 3}
    )
    monkeypatch.setattr(runtime_policy.PolicyStore, "read", lambda _: profile)
    monkeypatch.setattr(
        entry_controls,
        "read",
        lambda *_: SimpleNamespace(values={"sizing.remaining_budget_enabled": False}),
    )
    monkeypatch.setattr(
        context_module, "_risk_budget", lambda *_: events.append("risk")
    )
    monkeypatch.setattr(adaptive_leverage, "venue_facts", lambda *_args, **_kwargs: {})
    provider = object.__new__(context_module.BinanceDirectionalAccountContext)
    provider.scope = AccountScope("BINANCE", "qa-context", "SANDBOX", "FUTURES")
    provider.connect = lambda: pytest.fail("no real DB allowed")
    provider.clock = lambda: 100000
    provider.max_age = 15000
    provider.sentiment_max_age = 7200000
    provider.sentiment = object()

    def ratio(_target, **_kwargs):
        events.append("sentiment")
        return [{"symbol": "BTCUSDT", "shortAccount": "0.5", "timestamp": 100000}]

    provider._sentiment_cache = SimpleNamespace(public=provider.sentiment, read=ratio)

    def reserve(**scope):
        events.append("reserve")
        ticket = object()
        reserved.append((ticket, scope))
        return ticket

    provider.reserve_context = reserve

    def private(method, path, params, *, admission_ticket=None):
        reads.append(("private", method, path, params, admission_ticket, None))
        return [] if path.endswith("positionRisk") else {}

    def market(path, params, *, admission_ticket=None, account_id=None):
        reads.append(("public", "GET", path, params, admission_ticket, account_id))
        return {}

    def history(_signal):
        raise HistoryReached()

    provider.request, provider.public, provider.history = private, market, history
    return provider, events, reserved, reads


@pytest.mark.parametrize("adaptive", [False, True])
def test_complete_context_bundle_binds_each_get_and_preserves_two_fresh_positions(
    monkeypatch, adaptive
):
    provider, events, reservations, reads = context(monkeypatch, adaptive=adaptive)
    original_private, original_public = provider.request, provider.public
    for _ in range(2):
        with pytest.raises(HistoryReached):
            provider({"symbol": "BTCUSDT"})
    assert events == ["risk", "sentiment", "reserve"] * 2
    expected_count = 9 if adaptive else 7
    assert len(reads) == expected_count * 2
    assert len(reservations) == 2 and reservations[0][0] is not reservations[1][0]
    for index, (ticket, args) in enumerate(reservations):
        assert args == {
            "account_id": "qa-context",
            "symbol": "BTCUSDT",
            "adaptive": adaptive,
            "deadline_ms": 115000,
        }
        group = reads[index * expected_count : (index + 1) * expected_count]
        assert all(row[4] is ticket for row in group)
        assert all(row[5] == "qa-context" for row in group if row[0] == "public")
        assert Counter(row[2] for row in group)["/fapi/v3/positionRisk"] == 2
    assert provider.request is original_private and provider.public is original_public
    assert not vars(original_private) and not vars(original_public)


@pytest.mark.parametrize("stage", ["risk", "sentiment", "deadline", "reserve"])
def test_preflight_or_reservation_failure_never_starts_account_gets(monkeypatch, stage):
    provider, _events, reservations, reads = context(monkeypatch)

    def fail(*_args, **_kwargs):
        raise ValueError("QA_PRECHECK_FAILURE")

    if stage == "risk":
        monkeypatch.setattr(context_module, "_risk_budget", fail)
    elif stage == "sentiment":
        provider._sentiment_cache.read = lambda *_args, **_kwargs: []
    elif stage == "deadline":
        stamps = iter((100000, 115001))
        provider.clock = lambda: next(stamps)
    else:
        provider.reserve_context = fail
    with pytest.raises(ValueError):
        provider({"symbol": "BTCUSDT"})
    assert not reads and not reservations


@pytest.mark.parametrize(
    "real_error",
    [
        PublicMarketError("RATE_LIMITED"),
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=429),
        ConnectionError("QA DB failure"),
        ValueError("ADMISSION_CONTEXT_TICKET_INVALID"),
    ],
)
def test_parallel_real_failures_not_masked_by_earlier_ticket_wait(
    monkeypatch, real_error
):
    provider, _events, _reservations, reads = context(monkeypatch)
    original_private = provider.request

    def private(method, path, params, **kwargs):
        result = original_private(method, path, params, **kwargs)
        if path == "/fapi/v3/account":
            raise ValueError("ADMISSION_BUDGET_UNAVAILABLE")
        if path == "/fapi/v1/accountConfig":
            raise real_error
        return result

    provider.request = private
    with pytest.raises(type(real_error)) as caught:
        provider({"symbol": "BTCUSDT"})
    assert caught.value is real_error
    # All five parallel reads were awaited; no second position/history on failure.
    assert len(reads) == 6
    assert Counter(row[2] for row in reads)["/fapi/v3/positionRisk"] == 1


def test_legacy_context_with_no_reserver_does_not_add_transport_keywords(monkeypatch):
    provider, _events, _reservations, reads = context(monkeypatch)
    provider.reserve_context = None
    original_private, original_public = provider.request, provider.public
    provider.request = lambda method, path, params: original_private(
        method, path, params
    )
    provider.public = lambda path, params: original_public(path, params)
    with pytest.raises(HistoryReached):
        provider({"symbol": "BTCUSDT"})
    assert len(reads) == 7 and all(row[4] is None for row in reads)


def test_already_held_venue_symbol_stops_after_first_get_without_refunding_bundle(
    monkeypatch,
):
    provider, _events, reservations, reads = context(monkeypatch)
    original_private, original_reserve = provider.request, provider.reserve_context
    charged = []

    def reserve(**kwargs):
        charged.append(27)
        return original_reserve(**kwargs)

    def private(method, path, params, **kwargs):
        original_private(method, path, params, **kwargs)
        return [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}]

    provider.reserve_context, provider.request = reserve, private
    with pytest.raises(ValueError, match="^EXISTING_SYMBOL_POSITION$"):
        provider({"symbol": "BTCUSDT"})
    assert len(reads) == 1 and reads[0][2] == "/fapi/v3/positionRisk"
    assert len(reservations) == 1 and charged == [27]
    # Intentionally no refund: an issued/unknown GET is never recredited, and
    # unused reserved slots expire conservatively with the DB-minute window.


@pytest.fixture
def bundle_clock(shared, monkeypatch):
    """Keep actual PG locks/charges, deterministically choose an early DB minute."""
    from v2_core.shared_market import HostBudget

    with shared() as conn:
        window = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())/60)::bigint"
        ).fetchone()[0]
    clock = SimpleNamespace(now=window * 60000 + 1000)
    original = HostBudget._locked_state

    def locked(budget, conn):
        stored_window, used, market, blocked, _now = original(budget, conn)
        return stored_window, used, market, blocked, clock.now

    monkeypatch.setattr(HostBudget, "_locked_state", locked)
    return clock


class FactoryHTTP:
    """Per-connection deterministic venue responses; deliberately no sockets."""

    def __init__(self, clock):
        self.clock, self.calls, self.connections = clock, [], []
        self.on_connect = lambda: None

    def __call__(self, host, *, timeout):
        owner = self

        class Connection:
            closed = False
            status = 200

            def connect(self):
                owner.on_connect()

            def request(self, method, target, **_kwargs):
                assert method == "GET"
                self.path = urlsplit(target).path
                owner.calls.append((host, self.path))

            def getresponse(self):
                return self

            def getheader(self, _key, default):
                return default

            def read(self, limit):
                if self.path == "/futures/data/globalLongShortAccountRatio":
                    value = [
                        {
                            "symbol": "BTCUSDT",
                            "shortAccount": "0.5",
                            "timestamp": owner.clock.now,
                        }
                    ]
                elif self.path == "/fapi/v3/positionRisk":
                    value = []
                elif self.path == "/fapi/v1/leverageBracket":
                    value = [
                        {
                            "symbol": "BTCUSDT",
                            "brackets": [
                                {
                                    "notionalFloor": 0,
                                    "notionalCap": 100000,
                                    "initialLeverage": 20,
                                    "maintMarginRatio": "0.005",
                                }
                            ],
                        }
                    ]
                elif self.path == "/fapi/v1/ticker/bookTicker":
                    value = {
                        "symbol": "BTCUSDT",
                        "time": owner.clock.now,
                        "bidPrice": "100",
                        "askPrice": "100.01",
                        "bidQty": "1000",
                        "askQty": "1000",
                    }
                else:
                    value = {}
                return json.dumps(value).encode()[:limit]

            def close(self):
                self.closed = True

        conn = Connection()
        self.connections.append(conn)
        return conn


def factory_account(shared, clock, *, managed, adaptive):
    from market_fakes import ArchiveClient
    from test_v2_account_inventory import TelegramConnection, sink
    from test_v2_testnet_daemon_entry import settings

    from services.v2_testnet_daemon_entry import (
        TestnetProcessConfig,
        create_testnet_process,
    )
    from v2_core.account_risk import AccountPolicy, AccountRisk
    from v2_core.runtime_policy import PolicyStore

    redis = pytest.importorskip("redis")
    cache = redis.Redis(
        unix_socket_path=os.environ["V2_REDIS_TEST_SOCKET"], decode_responses=True
    )
    http = FactoryHTTP(clock)
    process = create_testnet_process(
        TestnetProcessConfig.from_mapping(settings(V2_SHARED_MARKET="true")),
        {
            "BINANCE_TESTNET_API_KEY": "qa-key",
            "BINANCE_TESTNET_API_SECRET": "qa-secret",
            "TG_NOTIFY_TOKEN": "12345:abcdefghijklmnop",
            "TG_NOTIFY_CHAT_ID": "-123",
        },
        connect=shared,
        redis_client=cache,
        clickhouse=ArchiveClient(),
        stop=threading.Event(),
        telegram_sink=sink(TelegramConnection()),
        clock_ms=lambda: clock.now,
        monotonic_ms=lambda: 0,
        signed_connection_factory=http,
        public_connection_factory=http,
        execution_guard=(
            lambda **_: pytest.fail("read-only test cannot acquire write guard")
        )
        if managed
        else None,
    )
    provider = process.daemon.pipeline.schedulers[0].context_provider
    while not hasattr(provider, "account"):
        provider = provider.delegate
    account = provider.account
    AccountRisk(shared).configure(
        account.scope,
        AccountPolicy("USDT", "10000", 10, 60000, "binance-closed-1m-v1", 90000),
        expected_version=0,
    )
    if adaptive:
        PolicyStore(shared, account.scope).patch(
            {
                "capital.enabled": True,
                "sizing.pool_fraction": "0.70",
                "portfolio_risk.enabled": True,
                "entry.force_isolated": True,
                "leverage.adaptive_enabled": True,
            },
            expected_version=0,
            reason="QA adaptive ticket bundle",
        )

    def history(_signal):
        # This integration verifies the real factory/PG/HTTP read set, not the
        # downstream sizing model. Stop only after the second fresh position GET.
        raise HistoryReached()

    account.history = history
    return process, account, http, cache


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("adaptive", [False, True])
def test_real_pg_factory_bundle_charged_once_and_shared_across_public_private_ports(
    shared, bundle_clock, managed, adaptive
):
    from v2_core.shared_market import AdmissionBudget

    process, account, http, cache = factory_account(
        shared, bundle_clock, managed=managed, adaptive=adaptive
    )
    try:
        private = account.request.request if managed else account.request
        assert isinstance(account.public.budget, AdmissionBudget)
        assert account.reserve_context.__self__ is account.public.budget
        assert private._permit.budget is account.public.budget
        assert account.sentiment.budget is not account.public.budget
        management = process.runtime.execution.submit.readiness.request
        management = management.request if managed else management
        assert management._permit.budget is not account.public.budget
        for _ in range(2):
            with pytest.raises(HistoryReached):
                account({"symbol": "BTCUSDT"})
        weight, count = (30, 9) if adaptive else (27, 7)
        with shared() as conn:
            assert conn.execute(
                "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
                ("demo-fapi.binance.com",),
            ).fetchone() == (weight * 2, weight * 2)
            assert conn.execute(
                "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
                ("fapi.binance.com",),
            ).fetchone() == (1, 1)
        sandbox = [path for host, path in http.calls if host == "demo-fapi.binance.com"]
        assert len(sandbox) == count * 2
        assert Counter(sandbox)["/fapi/v3/positionRisk"] == 4
        assert len(http.calls) == count * 2 + 1  # Only public sentiment is cached.
        assert all(conn.closed for conn in http.connections)
    finally:
        cache.close()


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("adaptive", [False, True])
def test_real_pg_factory_insufficient_bundle_starts_zero_sandbox_http(
    shared, bundle_clock, managed, adaptive
):
    _process, account, http, cache = factory_account(
        shared, bundle_clock, managed=managed, adaptive=adaptive
    )
    try:
        budget = account.public.budget
        assert budget.permit(1)
        remaining_short = 900 - (30 if adaptive else 27) + 1
        with shared() as conn:
            conn.execute(
                "UPDATE v2_egress_budgets SET used_weight=%s,market_weight=%s WHERE host=%s",
                (remaining_short, remaining_short, budget.host),
            )
        with pytest.raises(ValueError, match="^ADMISSION_BUDGET_UNAVAILABLE$"):
            account({"symbol": "BTCUSDT"})
        assert http.calls == [
            ("fapi.binance.com", "/futures/data/globalLongShortAccountRatio")
        ]
        with shared() as conn:
            assert conn.execute(
                "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
                (budget.host,),
            ).fetchone() == (remaining_short, remaining_short)
    finally:
        cache.close()


@pytest.mark.parametrize("port", ["signed", "public"])
@pytest.mark.parametrize("invalidation", ["minute", "penalty", "database"])
def test_real_pg_ticket_invalidation_during_connect_sends_no_http(
    shared, bundle_clock, monkeypatch, port, invalidation
):
    from v2_core.shared_market import AdmissionBudget

    budget, http = AdmissionBudget(shared, environment="SANDBOX"), HTTP()
    ticket = budget.reserve_context(
        account_id="qa-context",
        symbol="BTCUSDT",
        adaptive=False,
        deadline_ms=bundle_clock.now + 15000,
    )

    def invalidate():
        if invalidation == "minute":
            bundle_clock.now += 60000
        elif invalidation == "penalty":
            budget.penalize(60)
        else:

            def failed(_conn):
                raise ConnectionError("QA revalidation DB failure")

            monkeypatch.setattr(budget, "_locked_state", failed)

    http.on_connect = invalidate
    expected = ConnectionError if invalidation == "database" else ValueError
    with pytest.raises(expected) as caught:
        if port == "signed":
            signed(budget, http)("GET", "/fapi/v3/account", {}, admission_ticket=ticket)
        else:
            public(budget, http)(
                "/fapi/v1/exchangeInfo",
                {},
                admission_ticket=ticket,
                account_id="qa-context",
            )
    if invalidation != "database":
        assert str(caught.value) == "ADMISSION_BUDGET_UNAVAILABLE"
    assert http.closed and not http.requests
    with shared() as conn:
        assert conn.execute(
            "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
            (budget.host,),
        ).fetchone() == (27, 27)


def test_real_pg_ticket_slow_handshake_keeps_fresh_signature_and_original_deadline(
    shared, bundle_clock
):
    from v2_core.shared_market import AdmissionBudget

    budget, http = AdmissionBudget(shared, environment="SANDBOX"), HTTP()
    started = bundle_clock.now
    ticket = budget.reserve_context(
        account_id="qa-context",
        symbol="BTCUSDT",
        adaptive=False,
        deadline_ms=started + 15000,
    )
    http.on_connect = lambda: setattr(bundle_clock, "now", started + 6000)
    client = signed(budget, http, clock=lambda: bundle_clock.now)
    client("GET", "/fapi/v3/account", {}, admission_ticket=ticket)
    query = parse_qs(urlsplit(http.requests[0][0][1]).query)
    assert query["timestamp"] == [str(started + 6000)]
    assert ticket.deadline_ms == started + 15000
    with shared() as conn:
        assert conn.execute(
            "SELECT used_weight,market_weight FROM v2_egress_budgets WHERE host=%s",
            (budget.host,),
        ).fetchone() == (27, 27)
